# Contributing

## What this project is

A read-only index over a typed entity graph, built to be run by people who are not its authors. Nous Ergon runs **the published artifact plus private configuration** — never an internal fork. If you find a bug, we are hitting it too.

## The rules that shape every change

These are not style preferences; a change that breaks one of them will be sent back regardless of how well it works.

**The console renders; it never owns.** Every figure on the surface is a projection of a fact durable somewhere else — an object key, a registry file, an API. A number whose only home is this process is a number one crash removes. The single exception is view preferences (pins, saved facets, density): those are claims about the viewer, not about the system.

**No topology in source.** Every literal naming a bucket, ARN, host, port, path or component id comes from configuration. This is what makes the artifact usable by anyone but its author, and a topology literal in a pull request is a blocking review comment.

**One adapter per source, and adapters do not know each other.** An adapter that reads a second source, or reaches into another adapter's output to enrich its own, has become the coupling this boundary exists to prevent. Cross-source relations are formed by the index over entity identifiers.

**Absence renders as itself.** Zero, null, empty, never-ran and not-measured are five different facts. No data is never drawn as green and never drawn as nothing. A fetch that fails produces a state, not a blank region and not a stale value in normal styling.

**Every addressable state has a URL.** Every interaction that changes what is displayed writes to it, and a pasted URL reproduces the view on a cold load with no prior client state. State that lives only in client memory cannot be linked from an issue, cited as evidence, or returned to after a restart.

**A view exists only if it answers a question no other view answers.** Every view declares its question in one sentence, and that sentence is rendered on it. "An overview of X" is a topic, not a question — a view defending itself with a topic will accumulate anything topically adjacent to it.

## Adding an entity kind

The seven kinds are closed and change by pull request against the model, not by adding a case somewhere. If a fact you want to render is not a fact about one of them, say so in an issue first — either the model is incomplete, or the fact is not ready to render.

## Adding an adapter

The bar is that deleting it removes exactly its own entities: no view breaking, no other adapter changing, no route failing on a different kind. An adapter that reads a generic shape (a YAML registry directory, an object-store prefix, a CI API) belongs here even if you are its only user today. An adapter that hardcodes one organisation's layout is configuration that failed to become configuration.

## Roadmap and scope

This is a dogfooded tool, so **the authors' own use is the roadmap driver until there is a second real user.** That is a deliberate bound rather than indifference: it is what stops the project accreting configuration surface and abstraction for hypothetical users. A feature request grounded in your actual use of the tool is the thing that moves that line, and it is welcome — please describe the use, not just the feature.

## Pull requests

- State what changed and why, naming an issue or a concrete observed defect.
- Confirm the test suite passed before opening, not "will verify in CI".
- Keep a change to one purpose. Adjacency to something you are already touching is not a reason to widen it.
