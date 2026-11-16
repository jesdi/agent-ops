# 0017. Quarantine readability has one owner

Date: 2026-11-16

## Context

The claim gate, retry intent and console had separate parsers and disagreed about unreadable quarantine files.

## Decision

`dispatcher/failures.py::read_quarantine` owns quarantine parsing and exact field validation. `check_quarantine`, the retry intent and `Sources.quarantine_entries` all use it. Validate body fields before the console applies filename identity. This replaces the validator location in ADR 0008 (validate-quarantine-records-at-the-source), while preserving its requirement that every console consumer receives validated data.

## Consequences

Future quarantine consumers reuse the reader. The console depends on this dispatcher boundary rather than adding another parser or validator.
