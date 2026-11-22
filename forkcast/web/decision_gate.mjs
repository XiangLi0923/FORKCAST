// D09: generation identifies a set of conditions; sequence orders requests inside it.
export class DecisionGate {
  run = null;
  generation = 0;
  sequence = 0;
  latestSequence = 0;
  revision = 0;

  switchRun(snapshot) {
    this.run = snapshot.run_id;
    this.revision = snapshot.revision;
    this.changeConditions();
  }

  changeConditions() {
    this.generation += 1;
  }

  observe(snapshot) {
    if (snapshot.run_id === this.run) this.revision = Math.max(this.revision, snapshot.revision);
  }

  start() {
    this.latestSequence = ++this.sequence;
    return {run: this.run, generation: this.generation, sequence: this.sequence};
  }

  current(ticket) {
    return ticket.run === this.run && ticket.generation === this.generation &&
      ticket.sequence === this.latestSequence;
  }

  accept(ticket, snapshot) {
    if (!this.current(ticket) || snapshot.run_id !== this.run || snapshot.revision < this.revision) return false;
    this.observe(snapshot);
    return true;
  }
}
