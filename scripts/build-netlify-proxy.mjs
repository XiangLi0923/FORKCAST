import {mkdir, writeFile} from "node:fs/promises";

const publicHost = "aks-forkcast.netlify.app";
const upstream = process.env.FORKCAST_RENDER_ORIGIN ?? "";
const renderOrigin = /^https:\/\/[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.onrender\.com$/;

// public/_redirects is committed with the live Render origin, so a deploy
// never fails just because FORKCAST_RENDER_ORIGIN was not configured.
// Setting the variable rewrites the file for a different Render service.
const directory = new URL("../public/", import.meta.url);
await mkdir(directory, {recursive: true});
if (!upstream) {
  console.log("FORKCAST_RENDER_ORIGIN not set; keeping committed public/_redirects.");
} else {
  if (!renderOrigin.test(upstream)) {
    throw new Error("Set FORKCAST_RENDER_ORIGIN to the exact HTTPS origin assigned to the Render service.");
  }
  await writeFile(new URL("_redirects", directory), [
    "http://" + publicHost + "/* https://" + publicHost + "/:splat 301!",
    "/* " + upstream + "/:splat 200!",
    "",
  ].join("\n"), "utf8");
  console.log("Wrote public/_redirects for " + upstream);
}
