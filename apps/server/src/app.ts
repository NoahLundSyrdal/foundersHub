import Fastify from "fastify";
import { randomUUID } from "node:crypto";
import { EventEmitter } from "node:events";
import { resolve } from "node:path";
import {
  DeviceEventSchema,
  TranscriptSchema,
  ApprovalSchema,
  InterpretationSchema,
  proposalFingerprint,
  type DeviceCommand,
  type OfficeRequest,
  type EventResponse,
  type Interpretation,
} from "@office/contracts";
import { stations, office, root } from "./config.js";
import { Store } from "./db/store.js";
import { planPurchase } from "./procurement/plan.js";
import {
  mockInterpret,
  anthropicInterpret,
  type Interpreter,
} from "./agent/interpret.js";
import { mockSupplier, type Supplier } from "./integrations/supplier.js";
class HttpError extends Error {
  constructor(
    public statusCode: number,
    message: string,
  ) {
    super(message);
  }
}
export function buildApp(
  options: {
    databasePath?: string;
    interpreter?: Interpreter;
    supplier?: Supplier;
  } = {},
) {
  const app = Fastify({ logger: false, bodyLimit: 32768 });
  const store = new Store(
    options.databasePath ??
      resolve(root, process.env.DATABASE_PATH ?? ".data/office.sqlite"),
  );
  const provider = process.env.AGENT_PROVIDER ?? "mock";
  if (!["mock", "anthropic"].includes(provider))
    throw new Error("Unsupported AGENT_PROVIDER");
  if ((process.env.SUPPLIER_PROVIDER ?? "mock") !== "mock")
    throw new Error(
      "Only mock supplier is implemented; real purchasing is disabled",
    );
  const interpret =
    options.interpreter ??
    (provider === "anthropic" ? anthropicInterpret : mockInterpret);
  const supplier = options.supplier ?? mockSupplier;
  const updates = new EventEmitter();
  updates.setMaxListeners(50);
  const streams = new Set<import("node:http").ServerResponse>();
  function save(r: OfficeRequest) {
    store.save(r);
    updates.emit("updated", r.id);
  }
  let queue = Promise.resolve();
  function serial<T>(fn: () => Promise<T> | T): Promise<T> {
    const next = queue.then(fn);
    queue = next.then(
      () => undefined,
      () => undefined,
    );
    return next;
  }
  const required = (id: string) => {
    const r = store.get(id);
    if (!r) throw new HttpError(404, "Request not found");
    return r;
  };
  function command(r: OfficeRequest): DeviceCommand {
    return {
      station_id: r.station_id,
      text:
        r.status === "ordered"
          ? "SIMULATION: order confirmed"
          : r.status === "awaiting_input"
            ? "Describe what you need"
            : r.summary,
      color:
        r.status === "ordered" || r.status === "resolved"
          ? "green"
          : r.status === "failed"
            ? "red"
            : r.status === "awaiting_input"
              ? "blue"
              : "amber",
    };
  }
  const response = (r: OfficeRequest, duplicate = false): EventResponse => ({
    request: r,
    duplicate,
    command: command(r),
  });
  async function apply(
    r: OfficeRequest,
    text: string,
    itemId?: string,
  ): Promise<EventResponse> {
    const station = stations.find((s) => s.id === r.station_id)!;
    let parsed: Interpretation;
    r.status = "processing";
    r.trace = [];
    r.original_text = text;
    save(r);
    const progress = (step: { stage: string; detail: string }) => {
      r.trace!.push({ ...step, at: new Date().toISOString() });
      r.trace = r.trace!.slice(-40);
      r.updated_at = new Date().toISOString();
      save(r);
    };
    progress({
      stage: "received",
      detail: itemId
        ? "Sensor stock event received"
        : "Request received with station context",
    });
    try {
      parsed = InterpretationSchema.parse(
        itemId
          ? {
              intent: "purchase",
              item_id: itemId,
              quantity: 1,
              summary: `Low stock: ${itemId}`,
              question: null,
            }
          : await interpret(text, station, {
              openRequests: store
                .all()
                .filter(
                  (x) => x.id !== r.id && !["resolved"].includes(x.status),
                ),
              onProgress: progress,
            }),
      );
    } catch {
      r.status = "failed";
      r.summary =
        "Interpretation failed. Check provider configuration and try again.";
      r.original_text = text;
      r.updated_at = new Date().toISOString();
      save(r);
      return response(r);
    }
    progress({
      stage: "interpreted",
      detail: `Prepared ${parsed.intent} decision; checking backend rules`,
    });
    r.original_text = text;
    r.intent = parsed.intent;
    r.summary = parsed.summary;
    r.question = parsed.question;
    r.item_id = parsed.item_id;
    r.updated_at = new Date().toISOString();
    r.proposal = null;
    if (parsed.intent === "purchase") {
      const plan = planPurchase(
        station,
        parsed.item_id,
        parsed.quantity,
        store.all().filter((x) => x.id !== r.id),
      );
      if (plan.kind === "duplicate") {
        r.status = "resolved";
        r.summary = `Duplicate merged into ${plan.request.id}`;
        save(r);
        return response(plan.request, true);
      }
      if (plan.kind === "clarification") {
        r.status = "needs_clarification";
        r.question = plan.question;
      }
      if (plan.kind === "proposal") {
        r.proposal = plan.proposal;
        r.status = "awaiting_approval";
      }
    } else if (parsed.intent === "conversation") {
      r.status = "resolved";
      r.item_id = null;
      r.question = null;
    } else if (parsed.intent === "maintenance") {
      r.status = "maintenance_open";
      r.assigned_to = station.responsible_team;
    } else {
      r.status = "needs_clarification";
      r.question ??= "Please clarify what is missing or broken.";
    }
    save(r);
    return response(r);
  }
  app.setErrorHandler((err, _req, reply) => {
    const error = err as Error & { statusCode?: number; name?: string };
    reply
      .code(error.name === "ZodError" ? 400 : (error.statusCode ?? 500))
      .send({
        error:
          (error.statusCode ?? 400) < 500
            ? error.message
            : "Internal server error",
      });
  });
  app.get("/api/events", (req, reply) => {
    reply.hijack();
    const raw = reply.raw;
    streams.add(raw);
    raw.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });
    raw.write(": connected\n\n");
    const listener = (id: string) => {
      if (!raw.destroyed)
        raw.write(
          `event: requests.updated\ndata: ${JSON.stringify({ request_id: id })}\n\n`,
        );
    };
    updates.on("updated", listener);
    const heartbeat = setInterval(() => {
      if (!raw.destroyed) raw.write(": heartbeat\n\n");
    }, 15000);
    req.raw.on("close", () => {
      clearInterval(heartbeat);
      updates.off("updated", listener);
      streams.delete(raw);
    });
  });
  app.get("/api/health", () => ({
    ok: true,
    agent_provider: provider,
    supplier_provider: "mock",
    simulation: true,
  }));
  app.get("/api/stations", () => stations);
  app.get("/api/requests", () => store.all());
  app.get<{ Params: { station: string } }>(
    "/api/devices/:station/command",
    (req) => {
      if (!stations.some((s) => s.id === req.params.station))
        throw new HttpError(404, "Station not found");
      const latest = store
        .all()
        .filter((r) => r.station_id === req.params.station)
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
      return { command: latest ? command(latest) : null };
    },
  );
  app.post("/api/device-events", async (req) => {
    const event = DeviceEventSchema.parse(req.body);
    return serial(async () => {
      const existing = store.event(event.event_id);
      if (existing) return response(required(existing), true);
      const now = new Date().toISOString();
      const r: OfficeRequest = {
        id: randomUUID(),
        station_id: event.station_id,
        user_id: null,
        original_text: "",
        intent: null,
        status: "awaiting_input",
        summary: "Waiting for your request",
        question: null,
        item_id: null,
        proposal: null,
        supplier_reference: null,
        simulation: true,
        assigned_to: null,
        created_at: now,
        updated_at: now,
      };
      save(r);
      const result =
        event.type === "button_pressed"
          ? response(r)
          : await apply(
              r,
              event.payload.text ?? "",
              event.type === "stock_low" ? event.payload.item_id : undefined,
            );
      store.link(event.event_id, result.request.id);
      return result;
    });
  });
  app.post<{ Params: { id: string } }>(
    "/api/requests/:id/transcript",
    async (req) => {
      const { text } = TranscriptSchema.parse(req.body);
      return serial(() => {
        const r = required(req.params.id);
        if (
          r.proposal ||
          !["awaiting_input", "needs_clarification", "failed"].includes(
            r.status,
          )
        )
          throw new HttpError(
            409,
            "This request already has an action; create a new request",
          );
        return apply(r, text);
      });
    },
  );
  app.post<{ Params: { id: string } }>(
    "/api/requests/:id/approve",
    async (req) => {
      const approval = ApprovalSchema.parse(req.body);
      return serial(() => {
        const r = required(req.params.id);
        if (
          !r.proposal ||
          approval.proposal_fingerprint !== proposalFingerprint(r.proposal)
        )
          throw new HttpError(409, "Proposal changed; review it again");
        if (r.status === "ordered") return r;
        if (r.status !== "awaiting_approval")
          throw new HttpError(409, "Request is not awaiting approval");
        if (r.proposal.total_nok > office.max_order_nok)
          throw new HttpError(409, "Spending limit exceeded");
        const attempt = store.db
          .prepare("SELECT state FROM execution_attempts WHERE request_id=?")
          .get(r.id);
        if (attempt)
          throw new HttpError(
            409,
            "Execution already attempted; reconcile its outcome before retrying",
          );
        store.db
          .prepare("INSERT INTO execution_attempts VALUES(?,?,?,NULL)")
          .run(r.id, proposalFingerprint(r.proposal), "started");
        try {
          const result = supplier.placeOrder(r.proposal, r.id);
          r.supplier_reference = result.reference;
          r.status = "ordered";
          r.updated_at = new Date().toISOString();
          store.db.exec("BEGIN");
          try {
            save(r);
            store.db
              .prepare(
                "UPDATE execution_attempts SET state=?,reference=? WHERE request_id=?",
              )
              .run("confirmed", result.reference, r.id);
            store.db.exec("COMMIT");
          } catch (error) {
            store.db.exec("ROLLBACK");
            throw error;
          }
        } catch {
          r.status = "failed";
          r.summary =
            "Order outcome uncertain. Do not retry; reconcile the execution attempt.";
          save(r);
          throw new HttpError(409, r.summary);
        }
        return r;
      });
    },
  );
  app.post<{ Params: { id: string } }>(
    "/api/requests/:id/resolve",
    async (req) =>
      serial(() => {
        const r = required(req.params.id);
        if (!["maintenance_open", "ordered"].includes(r.status))
          throw new HttpError(
            409,
            "Only maintenance or ordered requests can be resolved",
          );
        r.status = "resolved";
        r.updated_at = new Date().toISOString();
        save(r);
        return r;
      }),
  );
  app.addHook("preClose", async () => {
    for (const stream of streams) stream.end();
  });
  app.addHook("onClose", async () => store.close());
  return app;
}
