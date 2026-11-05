export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(
    `/api${path}`,
    body === undefined
      ? undefined
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
  );
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data as T;
}

export type Board = { id: string; location: string; modules: string[]; equipment: string[] };
export type Person = { name: string; role: string; auto_approve_nok: number };
export type Pending = { tool_call_id: string; tool: string; args: Record<string, unknown>; details: { summary?: string; requested_by?: string } };
export type Task = { id: string; board_id: string; person: string; prompt: string; status: string; reply: string | null; pending: Pending[]; updated_at: string };
export type Order = { id: string; product_id: string; quantity: number; total_nok: number; status: string; ref: string };
export type Ticket = { id: string; board_id: string; summary: string; team: string; status: string };
export type Command = { board_id: string; text: string; color: string };
export type State = { tasks: Task[]; orders: Order[]; tickets: Ticket[]; commands: Record<string, Command> };
