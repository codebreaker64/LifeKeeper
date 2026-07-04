---
name: stride-threat-model
description: Performs a systematic STRIDE threat modeling assessment on the
current project's codebase and architecture. Use this when starting a new
implementation phase or reviewing existing components.
---

# STRIDE Threat Modeling Skill

## Goal
Guide the agent to analyze the workspace directory structure, configuration
files, and code files to produce a structured `threat_model.md` assessment.

## Instructions
1. **Analyze System Boundaries**: Map the entry points (tools, workflows,
   prompts) and data storage layers.
2. **STRIDE Evaluation**: Evaluate the system against the six STRIDE pillars:
   - **Spoofing**: Are caller identity boundaries verified before executing
     sensitive tool logic?
   - **Tampering**: Can users manipulate data flows, parameters, or underlying
     state?
   - **Repudiation**: Are critical transactions securely logged?
   - **Information Disclosure**: Are we risking leakage of PII, internal tokens,
     or raw stack traces?
   - **Denial of Service**: Are there rate limits on expensive database or LLM
     queries?
   - **Elevation of Privilege**: Can an unauthenticated user bypass access
     control to reach privileged tool actions?
3. **Output**: Generate a highly structured `threat_model.md` saved directly
   into the workspace root.
