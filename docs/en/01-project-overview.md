# Project overview

[中文](../zh-CN/01-项目介绍.md) · [Quick start](02-quick-start.md)

NovelFactory lets an author carry a story through several volumes while keeping track of who knows what, which draft an editor read, and which events have become part of the story. A work can keep a proposed plot turn on a branch while the author develops another chapter against the current revision.

## Following one scene

1. A planner proposes a destination for the next scene. The candidate records the world version and assumptions it needs.
2. The author or a qualified reviewer can select a plan. A role task receives a frozen context with its allowed material, current generation and tool permissions. A character who has yet to receive a message sees a different context from a character who has read it.
3. A writer produces a versioned draft. An editor reads that draft, identifies passages, and can request a targeted change. A later draft carries its own identity and review history.
4. A controller checks the world proposal against the current head, relevant changes, approvals and task authority. Acceptance appends an event and a revision. Publishing text and accepting a world transition have distinct receipts.
5. A later volume can adopt a reviewed continuity snapshot. A changed dependency can trigger replanning of affected work. The previous volume's world and the editor's original evidence remain available.

[Production pipeline](06-production-pipeline.md) · [World and knowledge model](03-system-architecture.md)

## Who operates what

| Participant | What they control |
| --- | --- |
| Author or project operator | The project binding, book scope, provider configuration, spending authority and decisions assigned to the controller. |
| Workshop manager | Employees, hierarchy, delegations, approved skills and their scope. Installation credentials connect the organization to NovelFactory. |
| Role host | One enrolled assignment. The host exposes authorized tools and overlays the frozen inputs with current authorization checks. |
| Editor or evaluator | A decision tied to the identified candidate, draft or instruction version. Decisions that require independence verify the underlying principals. |

For research, the fixture pipeline makes planning, knowledge cuts, revisions and recovery reproducible without sending a manuscript upstream. Readers can evaluate prose separately and attach the result to the exact manuscript they read. For a live project, configure an isolated project directory and an authorized model route. [Agent platform](04-agent-platform.md) · [Agent Runtime](05-agent-runtime.md) · [Evaluation](10-performance-and-evaluation.md)
