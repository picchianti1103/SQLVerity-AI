# Guided quickstart

This path verifies SQLVerity AI against the bundled synthetic PostgreSQL database. It does not need a
production database. Catalog exploration works without an LLM; SQL proposal generation is an optional
second stage that requires a configured provider.

Expected time: about 10 minutes for the local catalog path, plus provider setup if you want to generate
SQL.

## 1. Start the isolated demo

You need Docker Desktop or another Docker installation with Compose support.

```powershell
Copy-Item .env.example .env
```

On macOS or Linux, use `cp .env.example .env`.

Open `.env` and replace these development placeholders with independent random values:

- `SQLVERITY_BOOTSTRAP_API_KEY` — at least 32 characters;
- `SQLVERITY_POSTGRES_PASSWORD`;
- `SQLVERITY_DEMO_DB_PASSWORD`;
- `SQLVERITY_PREFLIGHT_SIGNING_KEY` — at least 32 characters.

Then start the stack:

```powershell
docker compose up --build
```

Keep this terminal open. In a second terminal, verify that both services are healthy:

```powershell
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/health
```

On macOS or Linux, use `curl --fail http://127.0.0.1:8000/health` for the second command. The health
request should succeed and `catalog-db` should report a healthy state.

## 2. Connect and create a workspace

Open [http://127.0.0.1:8000/ui](http://127.0.0.1:8000/ui).

1. Paste the `SQLVERITY_BOOTSTRAP_API_KEY` value from `.env` into **Bearer API key**, then select
   **Connect**. The browser keeps the key in memory only.
2. Expand **Create a new tenant**, enter `Quickstart`, and select **Create**.
3. Confirm that the new tenant is selected in **Workspace context**.

## 3. Register the bundled read-only source

Open **Data sources** and use these values:

| Field | Value |
| --- | --- |
| Name | `Synthetic commerce` |
| Source setup mode | `Direct database` |
| Dialect | `PostgreSQL` |
| Secret reference | `env://SQLVERITY_DEMO_DB` |

Select **Use recommended permissions**, then **Register source**. The secret reference is only an
opaque name: the password stays in the server environment and is never stored in the catalog.

With the new source selected, choose **Start introspection** under **Populate the catalog**. This reads
metadata using the bundled `sqlverity_demo_reader` login, which has `SELECT`-only access to the
synthetic database.

## 4. Verify the governed catalog

Open **Schema explorer**, select **Load schema**, and verify that the current catalog contains:

- `demo.customers` with `id`, `name`, `country_code`, and `created_at`;
- `demo.orders` with `id`, `customer_id`, `ordered_at`, `status`, and `total_amount`;
- the foreign-key relationship from `orders.customer_id` to `customers.id`;
- the comment describing `orders.total_amount` as an order total in EUR.

At this point you have exercised authentication, tenant isolation, an environment-backed secret
reference, read-only database introspection, and versioned catalog storage without making an AI call.

## 5. Optional: generate and run a proposal

Choose one provider in `.env`, add its credential and an explicit model ID, then restart the application
service. For example, the OpenAI block requires `SQLVERITY_LLM_PROVIDER`, `OPENAI_API_KEY`, and
`SQLVERITY_OPENAI_MODEL`. A local Ollama runtime can be used instead; see the
[provider configuration](configuration.md#llm-providers).

```powershell
docker compose up --build --detach sqlverity
```

Return to the console:

1. In **Privacy & AI**, select the configured provider.
2. Review the model, purpose, classification ceiling, residency, and retention declaration.
3. Acknowledge and save an authorization for **SQL proposal generation**.
4. In **Query Studio**, keep **Maximum privacy** selected and try:
   `Show paid order value by customer, highest first.`
5. Inspect the local preflight manifest before selecting **Send and generate SQL**.
6. Review the interpretation, SQL, and safety result.
7. Select **Inspect database plan**, then explicitly approve the exact query.
8. Select **Execute read-only** and inspect the result provenance.

Useful follow-up questions against the fixture are:

- `Show total order value by country, excluding cancelled orders.`
- `Which customers have no paid orders?`
- `Count orders by status.`

Generated SQL can vary by model. Do not approve a proposal unless the visible interpretation, SQL,
parameters, and database plan match the question.

## Stop and diagnose

Stop the services without deleting their volumes:

```powershell
docker compose down
```

If something fails, collect only non-sensitive diagnostics:

```powershell
docker compose ps
docker compose logs --no-color sqlverity
```

Remove tokens, connection strings, prompts, private schema details, and result rows before opening a
[bug report](https://github.com/picchianti1103/SQLVerity-AI/issues/new?template=bug.yml).
