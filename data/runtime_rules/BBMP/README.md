# BBMP runtime rules — status: PLACEHOLDER, do not rely on

`rules.json` in this directory is **not** the BBMP byelaws. It is a set of
four stub thresholds written to exercise the rule engine, and every one of
them is wrong as law. They are marked `[PLACEHOLDER - NOT A VERIFIED BBMP
THRESHOLD]` in their `description`, which propagates into every
`RuleResult.rule_description` a user would see, so a report generated today
cannot silently pass them off as legal advice.

## What is specifically wrong with them

| Rule | Stub says | Why that is wrong |
|---|---|---|
| `bbmp-min-front-setback-residential` | `setbacks.front >= 8.0 m`, `applies_when: {}` | BBMP front setback is a **table keyed on plot size and abutting road width**, not a single constant. 8.0 m is a high-rise figure; applying it to every plot fails essentially every small residential plot. |
| `bbmp-min-rear-setback-residential` | `setbacks.rear >= 4.5 m` | Same problem — the rear setback scales with plot depth and building height. |
| `bbmp-min-side-setback-residential` | `setbacks.right >= 4.5 m` | Two defects: the value is a large-plot figure, and it only constrains `setbacks.right`. **There is no rule for `setbacks.left` at all**, so a plan can violate its left setback and still pass. |
| `bbmp-maximum-coverage-residential` | `coverage <= 40 %` | BBMP permissible coverage varies by plot size and zone, and is commonly 60–75% for small residential plots. 40% fails ordinary compliant plans. |

Run against the bundled `PLAN6.pdf` these produce four FAILs on a plan that
was actually sanctioned by the authority.

## What replacing them requires

The engine and `RuntimeRuleDefinition` schema already support this — the gap
is purely the data, and the domain confirmation behind it:

1. **A plot-size / road-width lookup.** Setback and coverage limits are
   table-driven. `applies_when` already accepts a condition dict, so each
   band becomes its own rule (`applies_when: {plot.area: {"<=": 240}}`),
   which is why the architecture insists thresholds are never Python.
2. **A real citation per rule.** `citation` should name the gazette,
   clause and year. The sources are already in the repo under
   `data/regulations/BBMP/` (`bangalore_dcr.pdf`, the revised setback
   gazette, and the 2026 parking rules), and `backend/rase/` exists to draft
   rules from them via RAG — with `promote_draft` requiring a human to
   accept each one.
3. **`setbacks.left` coverage**, so both side setbacks are actually checked.

Until a person who can read the gazette signs off on the numbers, these stay
marked as placeholders. Authoring building-control thresholds from memory
would produce something that looks authoritative and is not.
