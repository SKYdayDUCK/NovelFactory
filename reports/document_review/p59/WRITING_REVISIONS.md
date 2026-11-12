# Independent editorial and bilingual review

Reviewer E entered after the documentation authors produced their first drafts. The reviewer had read-only responsibility for the public documents, used a separate agent context on the same workspace and had no role in authoring the public chapters or the example index. The implementer owns revisions. Locations below identify the first draft. Files in this directory are review evidence for the development branch.

## Change requests

| ID | First-draft location | Reader impact and requested revision |
| --- | --- | --- |
| E01 | zh-CN and en 06, paragraph after “Register a book”, line 9 | The linked quick start stops its temporary PostgreSQL cluster. Replace the registration prerequisite with the durable databases and NF_DATABASE_URL/NF_OBJECT_ROOT in chapter 08. |
| E02 | zh-CN and en 05, final accounting paragraph | “Subsequent dispatch must reconcile” can imply all other providers or books stop. Specify the original task/Attempt and its budget hold, and explicitly permit independently authorized work with capacity to proceed. |
| E03 | en 06 dependency-revalidation diagram, line 55 | “Hold” can instruct the caller to pause. Label the edge for a predicate that is true “Satisfied” and the opposite “Invalidated”; align with the Chinese conditions. |
| E04 | zh-CN 08, binding steps, line 52 | “建立工作” translates the data model's work as activity. Write “为每本小说建立作品记录、分支、预算和 Supervisor 绑定”. |
| E05 | en 06 opening, line 5 | The grammatical subject “A novel” purports to draft and commission itself. Give the actions to writers, reviewers and planners; keep their parallelism. |
| E06 | zh-CN and en 03, two-service sequence, lines 49–60 | The installation MCP only registers submit/get. Arrows for the Supervisor's internal task read, result storage and uncertain Attempt incorrectly target that port. Show the NF kernel/ledger separately and send the internal actions to it. Implementation in src/novel_factory/interfaces/delegation_stdio.py and application/supervisor.py. |
| E07 | en 01, steps and roles, lines 9 and 23 | “Proposes a destination” confuses the scene's goal with a location. “Overlays ... with current authorization checks” does not describe an operation. Use “proposes a goal” and “reads frozen inputs and checks current permissions before each tool call”. |
| E08 | zh-CN and en 01, line 10 | “The author or a qualified reviewer can select a plan” admits a self-approval reading. For CreativePlanningService.decide a non-null option requires APPROVE and a principal independent of the candidate owner. The owner's null decision returns DECLINED. State authority and independence directly. See src/novel_factory/application/creative_planning.py:279–292 and :325. |
| E09 | zh-CN and en 03, Creative Harness row and “Parallel work”; en 06 review section | The references list the adaptive tools but the architecture does not tell readers how to enable the harness, what the token budget omits, or how an instruction rollback affects existing inputs. Give one short practical account of scoped selection, independent evaluation, frozen inputs and historical world events, with links to the config and services. |
| E10 | zh-CN and en 08, numbered operator sequence, lines 51–53 | Activation precedes installing the profiles it checks. Put NF_RUNTIME_PROFILES setup before activating the policy and registering the work. Chapter 05 correctly requires every referenced profile at activation. |
| E11 | zh-CN and en 10, 200-agent capacity row, line 29 | The 200-agent historical P2 gate compares actual_call_start_rps to 95% of 1000. Its header-start rate is diagnostic. The frozen pair records call starts/s of 41.01218 and 35.38979 and wire header starts/s of 40.08036 and 27.79972. Report the two pairs with their boundaries. P3 open-1000 gates on wire start plus valid replies. See benchmarks/p2_behavioral_load.py:567–577, benchmarks/p3_open_arrival.py:136–141 and the preserved raw JSON. |

## What was examined

- Read both landing pages and all ten chapter pairs, including the operator language, node and edge labels, mode names, example provenance, caveats and navigation. Checked the per-tool purposes and shared table conventions against the bilingual API reference and the common executable example collection.
- Verified every example block is a literal match between Chinese and English. Each shell block parses with bash -n and each fenced JSON object parses. Identical commands alone cannot catch an incorrect prerequisite; E01 came from following the reader's path from sample to persistent book.
- Compared the complete registered endpoint set and example set with every API heading. Each of 98 entries appears once in each language and links to registered input and a replayable test-backed pair. The two languages share the same JSON, including 20 HTTP boundary fixtures, 8 HTTP-to-application mappings, 62 MCP fixtures and 8 supplements acting directly on handlers/services. No test boundary was reclassified as a live provider request.
- Compared the seven diagram pairs' edges, directions and branch points in addition to labels. The figures in chapter 03 and chapter 06 needed E06 and E03 despite matching in both languages.
- Cross-checked the chapter 10 definitions of an HTTP start and a valid result, the posted throughput and delays, the perf regressed cases, the original 950/s gate, the two 600-second runs, coverage of equal-share fairness and the unexecuted longer runs against the sealed measurements. The documented warm-buffer and pre-commit timing windows keep the measurements interpretable.
- Checked the writing prompt's named defensive and stock phrases. No listed literal remained in the 22 published documents. The material corrections above require more than vocabulary matching.

The technical reviewer independently checks the wire schema, permission semantics, generated errors and actual returns. The review is pending reread of the revised public text.


[Independent closure of revision requests](evidence/doc-independent-editor/FINAL_RECHECK.md) · [Separate technical correction log](TECHNICAL_ACCURACY_REVIEW.md)
