import { cpSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

// The standalone server needs the public static chunks next to its generated output.
cpSync(resolve(".next/static"), resolve(".next/standalone/.next/static"), { recursive: true });
process.env.HOSTNAME = process.env.TRACKER_HOST || "127.0.0.1";
process.env.PORT ||= "3000";
process.env.NEXT_TELEMETRY_DISABLED = "1";
await import(pathToFileURL(resolve(".next/standalone/server.js")).href);
