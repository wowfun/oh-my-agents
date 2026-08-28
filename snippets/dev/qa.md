Conduct a comprehensive, in-depth, and adversarial review of all uncommitted changes, covering areas including, but not limited to:
- scalability, extensibility, and maintainability, including algorithmic bottlenecks, unbounded work, lock contention, and architectural constraints
- over-engineering, unnecessary indirection, and overly defensive design
- avoidable performance and resource overhead, including redundant validation, cloning, allocation, I/O, IPC round trips, thread or task spawning, latency, and CPU cost
- unnecessary complexity, including unreachable defensive paths, duplicate logic, and convoluted control flow
- dead code, duplication, and maintenance burden

---

Conduct a comprehensive, in-depth, and adversarial audit of <repo>'s architecture, codebase, and test suite, and provide prioritized MUST and SHOULD recommendations, covering areas including, but not limited to:
...

---

Conduct a comprehensive, in-depth, and adversarial simplification audit of `<scope>`:
- Find unused or support-only surfaces, abandoned-feature residue, duplication, speculative generality, pass-through abstractions, needless nesting, redundant lifecycle machinery, misplaced defenses, and hand-rolled infrastructure replaceable by simpler established mechanisms.
- Reject changes that merely move complexity, reduce clarity, or alter required behavior.
- Preserve trust-boundary validation, security and accessibility controls, data safety, resource cleanup, and intentional seams.
