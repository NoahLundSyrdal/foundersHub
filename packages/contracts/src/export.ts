import { writeFileSync } from "node:fs";
import { z } from "zod";
import { DeviceEventSchema } from "./index.js";
writeFileSync(
  new URL("../device-event.schema.json", import.meta.url),
  JSON.stringify(z.toJSONSchema(DeviceEventSchema), null, 2) + "\n",
);
