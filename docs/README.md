# Word documentation

The checked-in deliverables are:

- `Quality_Lead_Costing_Guide.docx` — five pages, reference tariff tables,
  formulas, synthetic batch budgets, reuse effects and infrastructure scope.
- `Quality_Lead_Scoring_Guide.docx` — five pages, current policy weights, evidence
  and contact rules, growth recency, qualification caps and computed examples.

The costing document reports **unconfirmed repository default rates**, not a live
Azure quote or invoice. Confirm the actual region/deployment/contract and FX rate
before approving a budget. No customer usage or credentials are embedded.

To regenerate from the repository root, use the backend environment with
`python-docx==1.2.0` installed and run:

```bash
backend/.venv/bin/python docs/generate_documents.py
```

The generator reads repository pricing constants without loading application
configuration, and computes the scoring examples with the actual policy.
Review the prose and date when regenerating after policy changes. For layout
validation, open in Word or render with LibreOffice; the generated docs have
repeated table headings, non-splitting rows and A4 page layouts.
