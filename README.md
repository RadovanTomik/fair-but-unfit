# fair-but-unfit

Repository for storing scripts and queries relevant to the FAIR but unfit publication.

## Contents

### `quality-checks/qc_synthea.json`

The paper's 10 Synthea OMOP data quality checks (C1.1–C1.2, CO2.1–CO2.2, P3.1–P3.2,
CC4.1–CC4.2, CU5.1–CU5.2) as a JSON array, ready to upload to a
[Data Quality Framework](https://github.com/BBMRI-cz/data-quality-framework) agent.
Each entry is one Agent `QualityCheckCreateDTO` payload (`name`, `description`,
`query` + `type: "SQL"`, `warningThreshold`, `errorThreshold`). Every check returns the
`person_id` of each affected patient; the agent reports the share of affected patients.
To edit a check, edit this file and re-run the uploader (idempotent, matches by `name`).

### `quality-checks/qc_crc.json` and `quality-checks/category_crc.json`

The same 10-check structure adapted for the CRC-Cohort OMOP CDM v5.4 dataset
(diagnosis/specimen-oriented checks, tumour-location mapping check CC4.2, and
CU5.2 = *specimen not collected within one year of primary diagnosis*).
Upload against a CRC-backed agent with:

```bash
python3 scripts/upload_quality_checks.py --agent-url https://<crc-agent-host> \
    --checks quality-checks/qc_crc.json --category-file quality-checks/category_crc.json
```

### `scripts/upload_quality_checks.py`

Uploads the checks to an agent. Python 3.9+, no dependencies:

```bash
python3 scripts/upload_quality_checks.py --agent-url https://<agent-host>
# password via --password, $DQF_AGENT_PASSWORD, or an interactive prompt
# --dry-run prints what would change without touching the agent
```

Matching is by exact check name: existing checks are updated, the rest created.
The script also makes sure the category defined in
[`quality-checks/category.json`](quality-checks/category.json) (`{"name": "Synthea",
"colorHex": "#2563EB"}`) exists on the agent (created if missing) and assigns every
uploaded check to it. Point `--category-file <path>` at another JSON to use a
different category, or pass `--no-category` to upload checks without grouping.

**Live instance:** the checks are currently uploaded to the DQF Synthea agent
(`degraded` schema of the paper's OMOP database, see `deployments/dqf-synthea-agent`
in the companion `RadovanTomik/hermes` repository).
