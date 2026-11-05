import { writeFileSync } from "node:fs";
import { z } from "zod";
import { DeviceEventSchema, FacilityReadingSchema, FacilityRuleSchema, StationActionSchema, StationActionClaimSchema, StationActionAcknowledgementSchema } from "./index.js";
writeFileSync(
  new URL("../device-event.schema.json", import.meta.url),
  JSON.stringify(z.toJSONSchema(DeviceEventSchema), null, 2) + "\n",
);
for (const [name, schema] of [["facility-reading", FacilityReadingSchema], ["facility-rule", FacilityRuleSchema],
  ["station-action", StationActionSchema], ["station-action-claim", StationActionClaimSchema],
  ["station-action-acknowledgement", StationActionAcknowledgementSchema]] as const) {
  writeFileSync(new URL(`../${name}.schema.json`, import.meta.url), JSON.stringify(z.toJSONSchema(schema), null, 2) + "\n");
}
