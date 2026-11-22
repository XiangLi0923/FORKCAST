import {mkdir, writeFile} from "node:fs/promises";

const publicHost = "aks-forkcast.netlify.app";
const upstream = process.env.FORKCAST_RENDER_ORIGIN ?? "";
const renderOrigin = /^https:\/\/[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.onrender\.com$/;
if (!renderOrigin.test(upstream)) {
  throw new Error("Set FORKCAST_RENDER_ORIGIN to the exact HTTPS origin assigned to the Render service.");
}

const directory = new URL("../dist/netlify/", import.meta.url);
await mkdir(directory, {recursive: true});
await writeFile(new URL("_redirects", directory), [
  "http://" + publicHost + "/* https://" + publicHost + "/:splat 301!",
  "/* " + upstream + "/:splat 200!",
  "",
].join("\n"), "utf8");
