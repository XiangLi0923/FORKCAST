import {test} from "node:test";
import assert from "node:assert/strict";
import {DecisionGate} from "../forkcast/web/decision_gate.mjs";

const ref = (run_id, revision) => ({run_id, revision});

test("D09 only request 11 applies when 11 reaches the screen before request 10", () => {
  const gate = new DecisionGate();
  gate.switchRun(ref("runA", 1));
  for (let i = 1; i <= 9; i++) gate.start();
  const ten = gate.start();
  const eleven = gate.start();
  assert.equal(ten.sequence, 10);
  assert.equal(eleven.sequence, 11);
  assert.equal(ten.generation, eleven.generation);
  assert.ok(gate.accept(eleven, ref("runA", 2)));
  assert.equal(gate.accept(ten, ref("runA", 2)), false);
});

test("D09 changed conditions, old runs and regressed/in-flight revisions cannot apply", () => {
  const gate = new DecisionGate();
  gate.switchRun(ref("runA", 5));
  const old = gate.start();
  const nextGeneration = gate.generation + 1;
  gate.changeConditions();
  const next = gate.start();
  assert.equal(gate.generation, nextGeneration);
  assert.equal(gate.accept(old, ref("runA", 6)), false);
  gate.observe(ref("runA", 7));
  assert.equal(gate.accept(next, ref("runA", 6)), false);
  assert.ok(gate.accept(next, ref("runA", 7)));
  gate.switchRun(ref("runB", 1));
  assert.equal(gate.accept(next, ref("runA", 7)), false);
  assert.ok(gate.accept(gate.start(), ref("runB", 1)));
});
