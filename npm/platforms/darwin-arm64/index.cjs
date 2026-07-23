"use strict";

const path = require("node:path");

module.exports = Object.freeze({
  launcher: path.join(__dirname, "bin", "aizim-launcher"),
  platformManifest: path.join(__dirname, "manifest", "platform.json"),
});
