# Human roles in Lean-native research

## Reading Kevin Buzzard's FLT response

In his September 4, 2026 post, Kevin Buzzard congratulated Anthropic and reported compiling and
checking its FLT artifact. He distinguished that formalization from his project's modern proof,
continuing Mathlib contributions, and an explorable document for human readers. He saw substantial
value in automatic checking while retaining mathematical-library design and human understanding
as project outcomes. This is a response to a formalization milestone, not evidence of a new
mathematical discovery or an allegation of stolen work.

Sources:

- [Buzzard's original post](https://xenaproject.wordpress.com/2026/09/04/flt-anthropic-has-beaten-me-to-it/)
- [Anthropic's account](https://www.anthropic.com/research/formalizing-fermats-last-theorem)

## Product consequences for Aizim

Human work should have concrete inputs, outputs, and attribution:

| Human activity | Recorded artifact | What it establishes |
| --- | --- | --- |
| Choose a question and its intended scope | Immutable target statement and source references | The question being investigated |
| Choose definitions and useful interfaces | Definition/library-design contribution | A person's account of the mathematical design |
| Compare approaches or find counterexamples | Strategy contribution, scoped memory, inbox guidance | Traceable direction and reasons for changing course |
| Check mathematical meaning | Semantic review bound to target/evidence hashes | A recorded review of the formalization's intended meaning |
| Judge value and reusability | Research-value and library-quality reviews | Scholarly judgement distinct from formal validity |
| Explain the proof | Exposition linked to a target and its dependencies | A human-facing account of the argument |
| Decide how to present results | Release review and local export | An explicit record of presentation judgement |

AI can assist each activity. Attribution should describe actual contributions rather than
reserve artificial manual chores for humans. Routine proof attempts can remain autonomous;
operator guidance is recorded and consumed at round boundaries. The contribution ledger gives
human and machine work separate records without claiming that contribution counts measure value.

## Independent claims

Keep the following questions separate in both data and UI:

1. Did Lean accept this declaration under the recorded environment and assumptions?
2. Does that declaration mean the researcher's intended statement?
3. Is the result new, useful, general, or explanatory?
4. Who formulated, developed, formalized, reviewed, or explained it?
5. What independent evidence supports a priority or public-release claim?

The current workflow records (1), records operator reviews for (2) and (3), and accepts declared
contributions for (4). Its local hashes and timestamps are not independent evidence for (5).
An eventual scientific-priority protocol integration should receive a deliberately selected
artifact or commitment and preserve the researcher's chosen disclosure mode. A local author
name must not be represented as an authenticated scholarly identity.

## Acceptance

Changing a target creates a new research record. A human `accept` review cannot promote an
unproved statement. A restarted run retains prior contributions, feedback, and guidance.
The exported research record lets a reader inspect statements and dependencies alongside
explanations and evidence, while preserving explicit limits on identity and novelty claims.
