# 06 — Adjudication pilot: is the docs_drift corpus worth an LLM stage?

Rather than build an adjudication pipeline on faith, two pages from the
`docs_drift` corpus were adjudicated by hand — doing exactly what the LLM stage
would do — to measure yield first.

**Bar set in advance:** a candidate must be a **contradiction** (docs say X, code
says not-X), not an omission. Spring deliberately does not document every method,
and the harvested review corpus shows this team rejecting unsolicited editorial
changes outright.

## Page 1 — `data-access/orm/jpa.adoc` (21 signature changes): NO CANDIDATE

- All 13 bean properties the page documents (`persistenceXmlLocation`,
  `bootstrapExecutor`, `dataSourceLookup`, `packagesToScan`, …) still exist on the
  classes it documents. No contradiction.
- `LocalEntityManagerFactoryBean` gained `PersistenceConfiguration` constructors
  (Jakarta Persistence 3.2). The page documents only the persistence-unit-name
  path — an **omission**, and the documented approach still works. Below the bar.
- The example `persistence.xml` uses `xmlns="http://java.sun.com/xml/ns/persistence"
  version="1.0"` while Spring 7 builds against `jakarta.persistence-api:3.2.0` and
  its own current fixtures use `https://jakarta.ee/xml/ns/persistence` version 3.2.
  This *looked* like a strong find, and was rejected on checking:
  `PersistenceXmlParsingTests` **actively asserts that the old descriptor still
  parses**, and Spring dropped schema validation deliberately for cross-version
  compatibility (`PersistenceXmlParsingTests.java:244`,
  `@Disabled("not doing schema parsing anymore for JPA 2.0 compatibility")`).
  Dated, not broken. Editorial, so below the bar.

## Page 2 — `core/beans/factory-nature.adoc` (8 signature changes): **CANDIDATE**

`factory-nature.adoc:420-431` renders the interface and describes it:

```java
public interface LifecycleProcessor extends Lifecycle {
    void onRefresh();
    void onClose();
}
```
> "It also adds **two** other methods for reacting to the context being refreshed
> and closed."

Reality — `spring-context/.../context/LifecycleProcessor.java`:

| line | member | |
|---|---|---|
| 26 | `public interface LifecycleProcessor extends Lifecycle` | |
| 33 | `default void onRefresh()` | |
| 42 | `default void onRestart()` | **`@since 7.0`** |
| 52 | `default void onPause()` | **`@since 7.0`** |
| 62 | `default void onClose()` | |

Added 2025-08-01 in `149d468ce4` ("Introduce
`ConfigurableApplicationContext.pause()` and `SmartLifecycle.isPauseable()`").
`ConfigurableApplicationContext.pause()` is public API `@since 7.0`
(`ConfigurableApplicationContext.java:244`). The page **never mentions pause**; its
only "restart" hits are generic prose predating the feature.

Three defects in one place:
1. The rendered interface is **incomplete** — 2 of 4 methods missing.
2. The prose states "**two** other methods". There are four. Factually wrong.
3. The methods are `default`, shown as abstract — materially misleading to anyone
   implementing the interface from the docs.

This is a contradiction, not an omission: the page makes a false claim about a
count and shows a definition that does not match the type. Objectively verifiable,
docs-only, small diff, reader-facing.

## Why this matters for the architecture

**`docs_dead_ref` could never have found this.** Every symbol named on the page
exists; `Lifecycle` itself is unchanged and correctly rendered. The defect is a
wrong count and an incomplete listing — invisible to any reference-resolution
check, and only reachable by reading prose against code.

That is the clearest evidence so far that the harvester → adjudication → candidate
chain earns its place alongside the deterministic miners, rather than duplicating
them.

## Yield

2 pages adjudicated, **1 candidate, 1 rejection.** Too small to extrapolate, but
the corpus is only 9 pages, so a full pass is cheap and bounded. Notably the hit
came from the page ranked *third*, not first — signature-change count is a churn
proxy, not a wrongness proxy, exactly as the harvester docstring warns.

Also of note: the strongest-looking lead on page 1 (the JPA namespace) was killed
by checking Spring's own tests. An adjudicator that stops at "this looks outdated"
would have produced a false positive with a confident rationale. The
citation-required, reject-by-default framing is not decoration.

## Bug found in my own verification

A `zsh` shell helper computed setter names as
`local prop=$1 file=$2 setter="set${prop:0:1}…"`. **zsh evaluates all right-hand
sides before assigning**, so `setter` became the literal `"set"` and every lookup
failed — reporting 13 of 13 documented properties as missing. Bash would have
worked. Caught immediately because a 100% failure rate is implausible; the same
reflex the miners' self-checks encode.
