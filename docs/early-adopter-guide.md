# Early-adopter evaluation

SQLVerity AI needs evidence from real workflows more than a longer feature list. This guide turns a
30-minute test into feedback that can change the product.

Use synthetic data or metadata that your organization has approved for testing. Do not paste private
schemas, credentials, prompts, result rows, or security findings into a public issue.

## Suggested session

### 0–10 minutes: first value

Follow the [guided quickstart](quickstart.md) through catalog introspection. Record:

- time from cloning the repository to seeing the demo schema;
- any step that required guessing;
- whether the relationship and column meaning were understandable.

### 10–20 minutes: trust path

If you have an approved provider, generate one proposal and inspect each boundary:

1. the exact preflight transfer manifest;
2. the interpreted intent;
3. the proposed SQL and validation result;
4. the database plan;
5. the approval and execution steps;
6. result provenance.

Record the first point where you would no longer be comfortable proceeding with a real read-only data
source.

### 20–30 minutes: your use case

Describe one question from your actual work without sharing its data. A useful description includes:

- the role asking the question;
- the database dialect and approximate schema size;
- why existing BI, SQL, or chatbot tools are insufficient;
- which review or audit evidence is mandatory;
- what would make a four-week pilot successful.

Submit the result through the
[early-adopter feedback form](https://github.com/picchianti1103/SQLVerity-AI/issues/new?template=early-adopter.yml).

## Signals that matter

The project will prioritize repeated evidence over isolated feature requests. The strongest signals are:

- multiple users independently encounter the same blocked workflow;
- setup cannot reach a first inspected catalog within 15 minutes;
- a reviewer cannot determine what leaves the deployment or why SQL is executable;
- a supported dialect fails against a realistic, synthetic reproduction;
- a team is willing to run a time-bounded pilot and define its pass/fail criteria.

Positive feedback is welcome, but a concrete point of confusion is usually more valuable.
