#!/usr/bin/env node
// Parse every tracked src/*.js the way the browser does, and exit 1 if one does not.
//
// The pages load these as classic <script> files, so a syntax error in one is not a
// build failure anywhere: the file simply never runs, and the page comes up with its
// buttons dead and its dynamic text missing. The sister site justelesrecospsy shipped
// exactly that (a stray `});` left behind by a removed listener) past its own syntax
// check, because `node --check <path>` guesses the module type and the guess passed
// the broken file. So the source is parsed explicitly as a classic script with
// vm.Script rather than handed to `node --check` by path.
//
// Run by .githooks/pre-push and by deploy.sh's pre-flight. Needs nothing but node.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { Script } from "node:vm";

const files = execFileSync("git", ["ls-files", "src/*.js"], { encoding: "utf8" })
  .split("\n").filter(Boolean);
let failed = 0;
for (const file of files) {
  try {
    new Script(readFileSync(file, "utf8"), { filename: file });
  } catch (error) {
    failed += 1;
    console.error(`${file} does not parse as a classic script:\n  ${error.message}`);
  }
}
if (failed) {
  console.error(`check_js: ${failed} of ${files.length} file(s) would not run in the browser.`);
  process.exit(1);
}
console.log(`check_js: all ${files.length} src/*.js files parse.`);
