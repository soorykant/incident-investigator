## AI Engineer Assignment

## Build a Multi-Agent Production Incident Investigation System

## 1. Objective

Build a multi-agent AI system that investigates production incidents, identifies probable root causes, and recommends evidence-backed corrective actions.

The system receives an incident report and has access to simulated logs, performance metrics, deployment history, service dependencies, and historical incident reports.

The data may be incomplete, contradictory, or misleading. The system must investigate rather than simply generate an answer.

## 2. Core Requirements

## A. Multi-agent architecture

Implement at least five distinct agents:

- Orchestrator: Plans and coordinates the investigation.
- Observability Agent: Investigates logs and metrics.
- Dependency Agent: Examines deployments, configuration changes, and service dependencies.
- Reasoning Agent: Develops and compares competing root-cause hypotheses.
- Critic Agent: Challenges conclusions and requests further investigation when evidence is insufficient.
- Reporting Agent: Produces the final investigation report and remediation recommendations.

Agents must exchange structured results and genuinely collaborate. Simply chaining multiple LLM calls with different prompts is insufficient.

## B. Tool use

Implement tools for querying logs, metrics, deployment history, service dependencies, and historical incidents.

Tools must have structured inputs and outputs, error handling, and simulated failures such as timeouts or incomplete results.

## C. Orchestration and reliability

Implement:

- Parallel execution of independent tasks.
- Shared investigation state and task dependencies.
- Feedback loops for gathering additional evidence.
- Bounded retries, execution limits, and termination conditions.
- Recovery from agent or tool failures.

## D. Evidence-based conclusions

The final report must include ranked hypotheses, supporting and contradicting evidence, confidence levels, and recommended actions.

The system must distinguish facts from assumptions and allow an inconclusive outcome when evidence is insufficient.

## 3. Evaluation

Create at least 10 test incidents, including misleading evidence, conflicting observations, and tool failures.

Compare your multi-agent implementation against a single-agent baseline using the same tools.

Measure root-cause accuracy, evidence grounding, tool-call count, latency, and token consumption.

## 4. Deliverables

1. Runnable implementation with setup instructions.
2. Architecture diagram and explanation of agent coordination.
3. Synthetic incident dataset and automated tests.
4. Evaluation results comparing both approaches.
5. A technical note (maximum 500 words) explaining key decisions, trade-offs, limitations, and potential improvements.

## 5. Constraints

- Any language, LLM provider, and framework is allowed.
- AI coding assistants are permitted. Include the coding assistant transcript in submission
- No hardcoded answers for individual test cases.
- No polished frontend required.
- Document incomplete functionality and external dependencies.