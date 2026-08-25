# Clinical trial site feasibility - Snowflake hands-on lab

Assets for a one-hour hands-on lab that takes ten clinical trial site feasibility
surveys from PDF to a governed, question-answerable data product on Snowflake.

Everything here is **synthetic**. Every site, investigator, institution and study
code is fictional, generated for teaching purposes. There is no patient data and no
real-world trial data of any kind.

## What the lab does

Ten site feasibility surveys arrive as PDFs in two different layouts - long-form
prose and tick-box grids. Over six steps the lab:

1. Extracts structured capability data from the PDFs with `AI_EXTRACT`
2. Normalises free-text equipment names against a controlled vocabulary and scores
   the result against ground truth
3. Parses, chunks and indexes the same documents for semantic search with Cortex
   Search
4. Builds a semantic view so the numbers can be asked about in plain English
5. Puts a Cortex Agent over both the structured and unstructured sides
6. Deploys a Streamlit app on container runtime that reads all of it

## Contents

| Path | What it is |
|---|---|
| `notebook/az_feasibility_lab.ipynb` | The lab itself. Runs top to bottom in a Snowflake workspace |
| `guide/AZ_HoL_Participant_Guide.docx` | Written companion to the notebook |
| `documents/out/*.pdf` | The ten survey documents, two layouts |
| `data/*.csv` | Reference data - sites, studies, enrolment, taxonomy, ground truth |
| `semantic/site_feasibility_test.sv.yaml` | Semantic model, deployed during step 4 |
| `streamlit/` | The four-page app deployed in step 6 |

## How these files reach a Snowflake account

They are not uploaded by hand. Snowflake clones this repository directly:

```sql
CREATE OR REPLACE GIT REPOSITORY HOL_REPO
  API_INTEGRATION = AZ_HOL_ASSETS_API
  ORIGIN = 'https://github.com/Alexander122776/AZ-HOL-2026-Q3.git';

ALTER GIT REPOSITORY HOL_REPO FETCH;

COPY FILES INTO @RAW.SURVEYS/ FROM @HOL_REPO/branches/main/documents/out/
  PATTERN = '.*[.]pdf';

ALTER STAGE RAW.SURVEYS REFRESH;
```

Two points that are easy to get wrong:

- The destination stages **must** be created with
  `ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')`. `AI_EXTRACT` cannot read a
  client-side-encrypted PDF, and it fails silently rather than erroring.
- `ALTER STAGE ... REFRESH` is not optional. Neither `PUT` nor `COPY FILES` updates a
  stage's directory table, and the lab reads `DIRECTORY()`.

## Requirements

- A Snowflake account with Cortex enabled and cross-region inference allowed
- A compute pool for the Streamlit container runtime
- An external access integration permitting egress to PyPI, because the app depends
  on `pypdfium2` which is not in the Snowflake Anaconda channel

## Licence

Apache 2.0. See [LICENSE](LICENSE).
