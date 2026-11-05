import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { config } from "dotenv";
import type { Product, Station } from "@office/contracts";
export const root = fileURLToPath(new URL("../../../", import.meta.url));
config({ path: root + ".env", quiet: true });
export const stations = JSON.parse(
  readFileSync(root + "config/stations.json", "utf8"),
) as Station[];
export const products = JSON.parse(
  readFileSync(root + "fixtures/products.json", "utf8"),
) as Product[];
export const office = JSON.parse(
  readFileSync(root + "config/office.json", "utf8"),
) as { max_order_nok: number; preferences: string[] };
