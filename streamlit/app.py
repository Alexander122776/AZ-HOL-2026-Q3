"""
Site Feasibility - Streamlit in Snowflake, container runtime (SPCS).

Four pages:
  Portfolio        KPI tiles, ranked shortlist, score explainer, requirements
                   heatmap, risk quadrant
  Document review  The source PDF rendered inline as page images, side by side with
                   what was read out of it, plus an AI explanation of any document
  Site detail      Every capability with confidence and provenance
  Ask              Cortex Agent chat with reasoning, SQL and inline charts

Visual approach: Streamlit's default widgets are what make a SiS app look cheap, so
they are used for interaction only. Everything that displays is hand-authored HTML
against a small design system, and charts are Altair styled to brand rather than
st.line_chart and friends.

Notes:
  - Every number is read live from Snowflake. Nothing is hardcoded.
  - Ratios are DIV0(SUM(x), SUM(y)) in SQL, never averaged in Python.
  - The agent is reached over the REST API using the OAuth token the platform
    writes into the container at /snowflake/session/token. No PAT, no proxy.
    The _snowflake module used by warehouse-runtime apps does not exist here.
  - Every LLM string is coerced with str() before rendering, or a dict payload
    renders as the literal text "[object Object]".
  - The survey PDFs are rasterised server side with pypdfium2 and shown as
    images. st.pdf renders client side, and every Streamlit in Snowflake app
    runs under a Content Security Policy that blocks the iframe and worker
    machinery a browser PDF viewer needs, so it never gets past its spinner.
"""

import json
import os
from io import BytesIO
from textwrap import dedent

import requests

import altair as alt
import pandas as pd
import streamlit as st
from snowflake.snowpark.context import get_active_session

# Weights decide how the Portfolio shortlist ranks sites. They sum to 1.0.
# These are a judgement about what the programme values, not a domain standard,
# which is why the app shows them on the page rather than hiding them in here.
# Weight capability at 0.90 and the only site meeting every requirement rises to
# first; weight enrolment and the highest-volume site wins despite its gap.
SHORTLIST_WEIGHTS = {
    "capability_coverage": 0.50,
    "planned_enrolment": 0.35,
    "startup_speed": 0.15,
}

# Extractions below this are flagged for human review on the Site detail page.
# 0.70 is evidence based: in testing, the one field AI_EXTRACT got wrong scored
# 0.32, while every correct field scored 0.65 or higher.
MIN_CONFIDENCE = 0.70
# -----------------------------------------------------------------------------

DB = "AZ_FEASIBILITY_DEMO"
# The FAST agent, not the full one. Same semantic view and search service, but no
# Python sandbox and a 45 second budget instead of 120. The sandbox is the biggest
# single contributor to latency and the app draws its own charts from SQL, so it
# buys nothing here. The full agent stays available for the notebook step, where
# a plotted answer is the point.
AGENT_PATH = f"/api/v2/databases/{DB}/schemas/ANALYTICS/agents/SITE_FEASIBILITY_AGENT_FAST:run"

# Design tokens
INK = "#0F1B2A"
MUTED = "#64748B"
LINE = "#E2E8F0"
CANVAS = "#F7F9FC"
BRAND = "#29B5E8"
DEEP = "#11567F"
GOOD = "#1B7F3B"
WARN = "#B45309"
BAD = "#A32259"
SERIES = ["#29B5E8", "#11567F", "#7D44CF", "#D45B90", "#5FB894", "#F5A623"]

st.set_page_config(page_title="Site Feasibility", page_icon="🧬",
                   layout="wide", initial_sidebar_state="collapsed")


def css():
    st.markdown(f"""
    <style>
      #MainMenu, header[data-testid="stHeader"], footer {{ visibility: hidden; height: 0; }}
      .stDeployButton, [data-testid="stToolbar"], [data-testid="stDecoration"] {{ display: none; }}
      .stApp {{ background: {CANVAS}; }}
      .block-container {{ padding: 1.1rem 2.2rem 3rem; max-width: 1560px; }}

      * {{ -webkit-font-smoothing: antialiased; }}
      html, body, [class*="css"] {{
        font-family: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
        color: {INK};
      }}

      .masthead {{
        display: flex; align-items: center; justify-content: space-between;
        padding: .2rem 0 1rem; border-bottom: 1px solid {LINE}; margin-bottom: 1.4rem;
      }}
      .masthead h1 {{
        font-size: 1.45rem; font-weight: 650; margin: 0; letter-spacing: -.02em;
        color: {INK};
      }}
      .masthead .sub {{ font-size: .82rem; color: {MUTED}; margin-top: .18rem; }}
      .chip {{
        font-size: .7rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
        color: {DEEP}; background: #E7F5FD; border: 1px solid #C5E8F8;
        padding: .3rem .7rem; border-radius: 999px;
      }}

      .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: .9rem; margin-bottom: 1.4rem; }}
      .kpi {{
        background: #fff; border: 1px solid {LINE}; border-radius: 12px; padding: 1.05rem 1.2rem;
        box-shadow: 0 1px 2px rgba(15,27,42,.04);
      }}
      .kpi .lab {{
        font-size: .68rem; font-weight: 600; letter-spacing: .07em; text-transform: uppercase;
        color: {MUTED};
      }}
      .kpi .val {{ font-size: 1.95rem; font-weight: 650; letter-spacing: -.03em; margin-top: .3rem; line-height: 1; }}
      .kpi .foot {{ font-size: .74rem; color: {MUTED}; margin-top: .4rem; }}

      .card {{
        background: #fff; border: 1px solid {LINE}; border-radius: 12px;
        padding: 1.15rem 1.3rem; box-shadow: 0 1px 2px rgba(15,27,42,.04);
        margin-bottom: 1.1rem;
      }}
      .card h3 {{
        font-size: .95rem; font-weight: 650; margin: 0 0 .2rem; color: {INK};
      }}
      .card .note {{ font-size: .78rem; color: {MUTED}; margin-bottom: .9rem; }}

      table.grid {{ width: 100%; border-collapse: collapse; font-size: .83rem; }}
      table.grid thead th {{
        text-align: left; font-weight: 600; font-size: .68rem; letter-spacing: .06em;
        text-transform: uppercase; color: {MUTED}; padding: 0 .7rem .55rem;
        border-bottom: 1px solid {LINE}; white-space: nowrap;
      }}
      table.grid tbody td {{
        padding: .62rem .7rem; border-bottom: 1px solid #F1F5F9; vertical-align: middle;
      }}
      table.grid tbody tr:last-child td {{ border-bottom: none; }}
      table.grid tbody tr:hover {{ background: #FAFCFE; }}
      td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
      td.rank {{ color: {MUTED}; font-variant-numeric: tabular-nums; width: 1.8rem; }}
      .site {{ font-weight: 600; }}
      .where {{ color: {MUTED}; font-size: .78rem; }}

      .bar {{ background: #EEF2F7; border-radius: 4px; height: 6px; width: 88px; overflow: hidden; }}
      .bar > i {{ display: block; height: 100%; border-radius: 4px; }}

      .tag {{
        display: inline-block; font-size: .68rem; font-weight: 600; padding: .16rem .5rem;
        border-radius: 5px; letter-spacing: .01em;
      }}
      .t-can {{ background: #E8F6EC; color: {GOOD}; }}
      .t-cannot {{ background: #FDEBF2; color: {BAD}; }}
      .t-inferred {{ background: #FFF4E5; color: {WARN}; }}
      .t-neutral {{ background: #F1F5F9; color: {MUTED}; }}

      .banner {{
        border-radius: 10px; padding: .8rem 1rem; font-size: .82rem; margin-bottom: .9rem;
        border: 1px solid;
      }}
      .b-warn {{ background: #FFFBF3; border-color: #FBDBA7; color: #7C4A03; }}
      .b-info {{ background: #F4F9FE; border-color: #C8E3F7; color: {DEEP}; }}

      .doc {{
        background: #FCFDFE; border: 1px solid {LINE}; border-radius: 10px;
        padding: 1rem 1.1rem; font-family: "SFMono-Regular", Consolas, monospace;
        font-size: .74rem; line-height: 1.6; white-space: pre-wrap;
        max-height: 460px; overflow-y: auto; color: #263449;
      }}

      div[data-baseweb="tab-list"] {{ gap: .3rem; border-bottom: 1px solid {LINE}; }}
      button[data-baseweb="tab"] {{ font-size: .84rem; font-weight: 550; }}

      /* Navigation. Styled to read as a tab strip rather than four loose buttons.
         Scoped to the row immediately after .nav-anchor so it does not catch the
         action buttons inside the pages. */
      .nav-anchor + div [data-testid="stHorizontalBlock"] {{
        border-bottom: 1px solid {LINE}; padding-bottom: .55rem; margin-bottom: .3rem;
        gap: .15rem;
      }}
      .nav-anchor + div button {{
        border: none !important; background: transparent !important;
        box-shadow: none !important; border-radius: 7px !important;
        color: {MUTED} !important; font-size: .84rem !important; font-weight: 550 !important;
        padding: .42rem .2rem !important; white-space: nowrap !important;
        min-height: 0 !important;
      }}
      .nav-anchor + div button:hover {{ background: #EDF2F7 !important; color: {INK} !important; }}
      /* Selected page. Light blue fill with a brand border, not Streamlit's
         default red - this is a navigation state, not a warning. The bare
         button[kind="primary"] rule is a safety net in case the sibling
         selector above stops matching after a Streamlit upgrade. */
      button[kind="primary"], .nav-anchor + div button[kind="primary"] {{
        background: #E3F4FD !important; color: {DEEP} !important;
        border: 1px solid {BRAND} !important; font-weight: 650 !important;
      }}
      button[kind="primary"]:hover, .nav-anchor + div button[kind="primary"]:hover {{
        background: #D2ECFB !important; color: {DEEP} !important;
        border: 1px solid {BRAND} !important;
      }}

      .sec {{
        font-size: .78rem; font-weight: 650; letter-spacing: .05em; text-transform: uppercase;
        color: {MUTED}; margin: .3rem 0 .55rem;
      }}
      .labnote {{
        font-size: .74rem; color: {DEEP}; background: #F4F9FE; border: 1px dashed #C8E3F7;
        border-radius: 7px; padding: .4rem .65rem; display: inline-block; margin-top: .5rem;
      }}

      /* Search result passages. A numbered rank, the page it came from, and
         the text itself set as prose rather than as a wall of monospace. */
      .psg {{
        background: #fff; border: 1px solid {LINE}; border-radius: 12px;
        padding: .8rem 1rem .9rem; margin-bottom: .6rem;
        box-shadow: 0 1px 2px rgba(15,27,42,.04);
      }}
      .psg-h {{
        display: flex; align-items: center; gap: .5rem; margin-bottom: .55rem;
        padding-bottom: .5rem; border-bottom: 1px solid {LINE};
      }}
      .psg-n {{
        width: 19px; height: 19px; border-radius: 50%; background: {DEEP};
        color: #fff; font-size: .64rem; font-weight: 700; flex: 0 0 19px;
        display: inline-flex; align-items: center; justify-content: center;
      }}
      .psg-p {{ font-size: .73rem; font-weight: 650; color: {INK}; }}
      .psg-s {{ margin-left: auto; font-size: .68rem; color: {MUTED}; }}
      .psg-t {{
        font-size: .8rem; line-height: 1.62; color: #2B3A4E; white-space: pre-wrap;
        max-height: 200px; overflow-y: auto;
      }}
      .pill {{
        display: inline-block; font-size: .66rem; font-weight: 600; color: {DEEP};
        background: #F1F7FD; border: 1px solid #DCEBF8; border-radius: 999px;
        padding: .15rem .55rem; margin: 0 .3rem .3rem 0;
      }}

      /* Chat. Cards with a little lift rather than Streamlit's flat rows, brand
         coloured avatars instead of the default coloured squares, and expanders
         quietened down so the answer is the loudest thing in the turn. */
      [data-testid="stChatMessage"] {{
        background: #fff; border: 1px solid {LINE}; border-radius: 14px;
        padding: .8rem 1rem; margin-bottom: .7rem;
        box-shadow: 0 1px 3px rgba(15,27,42,.05);
      }}
      [data-testid="stChatMessage"] p, [data-testid="stChatMessage"] li {{
        font-size: .87rem; line-height: 1.66;
      }}
      [data-testid="stChatMessageAvatarAssistant"] {{
        background: {DEEP} !important; color: #fff !important; border: none !important;
      }}
      [data-testid="stChatMessageAvatarUser"] {{
        background: #E3F4FD !important; color: {DEEP} !important; border: none !important;
      }}
      [data-testid="stChatMessage"] details {{
        border: 1px solid {LINE} !important; border-radius: 9px !important;
        background: {CANVAS} !important; margin-top: .35rem;
      }}
      [data-testid="stChatMessage"] summary p {{
        font-size: .74rem !important; font-weight: 620 !important; color: {MUTED} !important;
      }}
      [data-testid="stChatInput"] {{ border-radius: 12px; }}
    </style>
    """, unsafe_allow_html=True)


css()


@st.cache_resource
def sf():
    return get_active_session()


@st.cache_data(ttl=300, show_spinner=False)
def q(sql: str) -> pd.DataFrame:
    return sf().sql(sql).to_pandas()


@st.cache_data(show_spinner=False, ttl=60)
def have(schema: str, table: str) -> bool:
    """Does this table exist yet?

    The pipeline tables are built by the notebook, so on a fresh account they do
    not exist until the relevant step has been run. Checking first lets a page
    say which step is missing instead of failing with a SQL compilation error.
    The TTL is short so the page starts working as soon as the step is run.
    """
    return not q(f"""
        SELECT table_name FROM {DB}.INFORMATION_SCHEMA.TABLES
        WHERE table_schema = '{schema}' AND table_name = '{table}'
    """).empty


@st.cache_data(show_spinner=False, ttl=60)
def have_search(name: str) -> bool:
    """Does this Cortex Search service exist yet?

    Search services are not tables, so they are not in INFORMATION_SCHEMA.TABLES.
    Fails open: if the SHOW cannot be run for any reason, assume it is there and
    let the real call report the real error rather than blocking the page.
    """
    try:
        return not q(f"""SHOW CORTEX SEARCH SERVICES LIKE '{name}'
                          IN SCHEMA {DB}.ANALYTICS""").empty
    except Exception:                                              # noqa: BLE001
        return True


def needs_step(step: str, what: str) -> None:
    """Explain a missing pipeline table and stop rendering the page."""
    st.info(f"**{what} has not been built yet.** Run {step} of the notebook, "
            f"then reload this page.", icon=":material/info:")
    st.stop()


def masthead(title: str, sub: str, chip: str):
    st.markdown(
        f'<div class="masthead"><div><h1>{title}</h1><div class="sub">{sub}</div></div>'
        f'<div class="chip">{chip}</div></div>',
        unsafe_allow_html=True,
    )


def kpi_row(items):
    cells = "".join(
        f'<div class="kpi"><div class="lab">{lab}</div>'
        f'<div class="val" style="color:{col}">{val}</div>'
        f'<div class="foot">{foot}</div></div>'
        for lab, val, foot, col in items
    )
    st.markdown(f'<div class="kpis">{cells}</div>', unsafe_allow_html=True)


def card(title: str, note: str, body: str):
    st.markdown(
        f'<div class="card"><h3>{title}</h3><div class="note">{note}</div>{body}</div>',
        unsafe_allow_html=True,
    )


def bar(pct: float) -> str:
    col = GOOD if pct >= 0.85 else (WARN if pct >= 0.7 else BAD)
    return (f'<div style="display:flex;align-items:center;gap:.5rem">'
            f'<div class="bar"><i style="width:{pct*100:.0f}%;background:{col}"></i></div>'
            f'<span class="num" style="font-size:.78rem">{pct:.0%}</span></div>')


# -----------------------------------------------------------------------------
# Queries
# -----------------------------------------------------------------------------

def kpis():
    return q(f"""
        SELECT
          COUNT(DISTINCT CASE WHEN items_required > 0 THEN site_id END) AS sites_assessed,
          (SELECT COUNT(*) FROM {DB}.RAW.SITES)                         AS sites_total,
          (SELECT COUNT(*) FROM {DB}.RAW.DOCUMENT_MANIFEST)             AS documents,
          DIV0(SUM(items_available), SUM(items_required))               AS coverage,
          SUM(items_missing)                                            AS total_gaps,
          (SELECT AVG(confidence) FROM {DB}.ANALYTICS.FACT_CAPABILITY
            WHERE confidence IS NOT NULL)                               AS mean_confidence,
          (SELECT COUNT(*) FROM {DB}.ANALYTICS.FACT_CAPABILITY WHERE needs_review) AS review_items
        FROM {DB}.ANALYTICS.FACT_SITE_STUDY
        WHERE items_required > 0
    """).iloc[0]


def shortlist(w: dict) -> pd.DataFrame:
    return q(f"""
        WITH assessed AS (
          SELECT f.site_id, f.study_code, s.institution_name, s.city, s.country,
                 st.therapy_area, f.items_available, f.items_required, f.items_missing,
                 DIV0(f.items_available, f.items_required) AS coverage,
                 f.planned_enrolled, f.startup_weeks, f.competing_studies
          FROM {DB}.ANALYTICS.FACT_SITE_STUDY f
          JOIN {DB}.RAW.SITES   s  ON s.site_id    = f.site_id
          JOIN {DB}.RAW.STUDIES st ON st.study_code = f.study_code
          WHERE f.items_required > 0
        ),
        b AS (SELECT MIN(planned_enrolled) e_lo, MAX(planned_enrolled) e_hi,
                     MIN(startup_weeks) w_lo, MAX(startup_weeks) w_hi FROM assessed),
        -- The three inputs, each rescaled to 0-1 across the assessed sites so they
        -- are comparable before weighting. Coverage is already a ratio. Enrolment is
        -- min-max scaled. Startup is inverted first, because fewer weeks is better.
        parts AS (
          SELECT a.*,
                 ROUND(a.coverage, 4) AS part_requirements,
                 ROUND(DIV0(a.planned_enrolled - b.e_lo,
                            NULLIF(b.e_hi - b.e_lo, 0)), 4) AS part_enrolment,
                 ROUND(1 - DIV0(a.startup_weeks - b.w_lo,
                                NULLIF(b.w_hi - b.w_lo, 0)), 4) AS part_speed
          FROM assessed a CROSS JOIN b
        )
        SELECT parts.*,
               ROUND({w['capability_coverage']} * part_requirements
                   + {w['planned_enrolment']}   * part_enrolment
                   + {w['startup_speed']}       * part_speed, 4) AS score
        FROM parts
        ORDER BY score DESC
    """)


def heat():
    return q(f"""
        SELECT site_id, item_category,
               DIV0(SUM(IFF(capability = 'CAN', 1, 0)), COUNT(*)) AS coverage
        FROM {DB}.ANALYTICS.FACT_CAPABILITY
        WHERE is_required_for_study
        GROUP BY 1, 2
    """)


def coverage_detail():
    """The arithmetic behind the coverage headline, so the number can be explained."""
    return q(f"""
        SELECT SUM(items_required)  AS required,
               SUM(items_available) AS available,
               SUM(items_missing)   AS missing,
               COUNT(*)             AS sites,
               COUNT(CASE WHEN items_missing > 0 THEN 1 END) AS sites_with_gaps
        FROM {DB}.ANALYTICS.FACT_SITE_STUDY
        WHERE items_required > 0
    """).iloc[0]


@st.cache_data(show_spinner=False)
def pdf_bytes(doc_id: str) -> bytes:
    """Read the PDF out of the stage and into the app.

    Cached, because it is the same ten files for the whole lab and the container
    runtime shares its cache across every viewer session.
    """
    with sf().file.get_stream(f"@{DB}.RAW.SURVEYS/{doc_id}.pdf") as fh:
        return fh.read()


def pdf_url(doc_id: str) -> str:
    return q(f"""
        SELECT GET_PRESIGNED_URL(@{DB}.RAW.SURVEYS, '{doc_id}.pdf', 3600) AS url
    """).iloc[0].URL


def documents():
    return q(f"""
        SELECT m.document_id, m.site_id, m.study_code,
               s.institution_name, s.city, s.country,
               st.drug_name, st.therapy_area,
               (SELECT COUNT(*) FROM {DB}.EXTRACTED.PAGES p
                 WHERE p.document_id = m.document_id) AS pages
        FROM {DB}.RAW.DOCUMENT_MANIFEST m
        JOIN {DB}.RAW.SITES   s  ON s.site_id    = m.site_id
        JOIN {DB}.RAW.STUDIES st ON st.study_code = m.study_code
        ORDER BY m.document_id
    """)


def doc_pages(doc_id: str):
    return q(f"""
        SELECT page_index, page_text
        FROM {DB}.EXTRACTED.PAGES
        WHERE document_id = '{doc_id}' ORDER BY page_index
    """)


def doc_items(doc_id: str):
    return q(f"""
        SELECT item, item_category, capability, capability_source, confidence,
               name_similarity, extracted_as, was_normalised, needs_review,
               is_required_for_study
        FROM {DB}.ANALYTICS.FACT_CAPABILITY
        WHERE document_id = '{doc_id}'
        ORDER BY is_required_for_study DESC, item_category, item
    """)


def site_items(site_id: str):
    return q(f"""
        SELECT item, item_category, capability, capability_source, confidence,
               name_similarity, extracted_as, was_normalised, needs_review,
               is_required_for_study, document_id
        FROM {DB}.ANALYTICS.FACT_CAPABILITY
        WHERE site_id = '{site_id}'
        ORDER BY is_required_for_study DESC, item_category, item
    """)


def site_header(site_id: str):
    return q(f"""
        SELECT s.site_id, s.institution_name, s.city, s.country, s.region,
               s.institution_type, s.has_emr, s.gcp_trained,
               f.study_code, st.drug_name, st.therapy_area, st.indication, st.phase,
               f.items_available, f.items_required, f.items_missing,
               DIV0(f.items_available, f.items_required) AS coverage,
               f.planned_enrolled, f.planned_screened, f.patients_seen_per_year,
               f.startup_weeks, f.competing_studies
        FROM {DB}.RAW.SITES s
        LEFT JOIN {DB}.ANALYTICS.FACT_SITE_STUDY f ON f.site_id = s.site_id
        LEFT JOIN {DB}.RAW.STUDIES st ON st.study_code = f.study_code
        WHERE s.site_id = '{site_id}' AND f.items_required > 0
    """).iloc[0]


def assessed_sites():
    return q(f"""
        SELECT DISTINCT f.site_id, s.institution_name, s.city, s.country
        FROM {DB}.ANALYTICS.FACT_SITE_STUDY f
        JOIN {DB}.RAW.SITES s ON s.site_id = f.site_id
        WHERE f.items_required > 0 ORDER BY f.site_id
    """)


def explain_document(doc_id: str) -> str:
    """Ask a model to summarise a survey from its own text plus what was extracted.

    This is the 'new document' path: everything here is derived from the parsed
    text and the extraction output, so it works for a document nobody has read.
    """
    return q(f"""
        WITH body AS (
          SELECT LISTAGG(page_text, '\\n\\n') WITHIN GROUP (ORDER BY page_index) AS txt
          FROM {DB}.EXTRACTED.PAGES WHERE document_id = '{doc_id}'
        ),
        found AS (
          SELECT LISTAGG(item || ' = ' || capability
                   || IFF(capability_source = 'DERIVED_ABSENT', ' (inferred from absence)', ''),
                   '; ') WITHIN GROUP (ORDER BY item) AS items
          FROM {DB}.ANALYTICS.FACT_CAPABILITY
          WHERE document_id = '{doc_id}' AND is_required_for_study
        )
        SELECT SNOWFLAKE.CORTEX.COMPLETE('claude-4-sonnet',
          'You are briefing a clinical trial feasibility lead who has not read this '
          || 'site survey. Write four short bullet points, no preamble, no heading.\\n\\n'
          || 'Cover, in this order: what the site can do that the study needs; what it '
          || 'cannot do; anything the site said in free text that carries risk to the '
          || 'study timeline; and one clear recommendation.\\n\\n'
          || 'Use British spelling and short dashes, never long dashes. Do not invent '
          || 'anything that is not in the material below.\\n\\n'
          || 'REQUIRED ITEMS AS EXTRACTED:\\n' || f.items
          || '\\n\\nFULL SURVEY TEXT:\\n' || b.txt
        ) AS explanation
        FROM body b, found f
    """).iloc[0].EXPLANATION


@st.cache_data(show_spinner=False)
def pdf_page_images(doc_id: str, scale: float = 2.0) -> list:
    """Render the survey to PNGs here, in Python, rather than in the browser.

    st.pdf draws the document client side. Every Streamlit in Snowflake app runs
    under a Content Security Policy that blocks iframes and externally sourced
    worker scripts, which is exactly what a browser PDF viewer is built from, so
    the component loads and then sits on "Loading PDF..." indefinitely - handed a
    presigned URL or the raw bytes, it makes no difference. Rasterising server
    side takes the browser out of it: what reaches the page is an image, and
    nothing blocks an image. Cached, because it is the same ten files all lab.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(BytesIO(pdf_bytes(doc_id)))
    out = []
    for page in doc:
        buf = BytesIO()
        page.render(scale=scale).to_pil().save(buf, format="PNG")
        out.append(buf.getvalue())
    return out


# -----------------------------------------------------------------------------
# Agent
# -----------------------------------------------------------------------------

def _container_token() -> str:
    """The OAuth token Snowflake writes into every SPCS container.

    This is what authenticates the app to the REST API as the app's owner. It is
    injected by the platform, is short lived, and never leaves the container.
    """
    with open("/snowflake/session/token", "r", encoding="utf-8") as fh:
        return fh.read()


def ask_agent(question: str) -> dict:
    # Warehouse-runtime Streamlit calls agents through _snowflake.send_snow_api_request.
    # The container runtime has no _snowflake module at all, so on SPCS the supported
    # route is the REST API directly: the container's own OAuth token plus requests.
    # stream=False with Accept: application/json collapses the server-sent event
    # stream into one JSON object, which is the same shape the parser below expects.
    out = {"answer": "", "thinking": "", "code": [], "charts": [], "tools": [], "error": None}

    host = os.getenv("SNOWFLAKE_HOST")
    if not host:
        out["error"] = "SNOWFLAKE_HOST is not set, so the agent endpoint cannot be resolved."
        return out

    try:
        resp = requests.post(
            f"https://{host}{AGENT_PATH}",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {_container_token()}",
                "X-Snowflake-Authorization-Token-Type": "OAUTH",
            },
            json={
                "stream": False,
                "messages": [{"role": "user",
                              "content": [{"type": "text", "text": question}]}],
            },
            timeout=180,
        )
    except Exception as exc:                                       # noqa: BLE001
        out["error"] = str(exc)
        return out

    if resp.status_code != 200:
        out["error"] = f"The agent endpoint returned {resp.status_code}: {resp.text[:400]}"
        return out

    try:
        body = resp.json()
    except ValueError:
        out["answer"] = resp.text
        return out

    for ev in (body if isinstance(body, list) else [body]):
        if not isinstance(ev, dict):
            continue
        for blk in ev.get("content", []) or []:
            if not isinstance(blk, dict):
                continue
            kind = blk.get("type")
            if kind == "text":
                # Blank line between blocks. Joining them raw is what produced
                # "...capability recorded.Only one site can..." - the agent's
                # opening line and its answer welded into one paragraph.
                txt = str(blk.get("text") or "").strip()
                if txt:
                    out["answer"] += ("\n\n" if out["answer"] else "") + txt
            elif kind == "thinking":
                th = blk.get("thinking") or {}
                out["thinking"] += str(th.get("text") if isinstance(th, dict) else th)
            elif kind == "tool_use":
                tu = blk.get("tool_use") or {}
                nm = str(tu.get("name") or "")
                if nm:
                    out["tools"].append(nm)
                inp = tu.get("input") or {}
                if isinstance(inp, dict):
                    if inp.get("sql"):
                        out["code"].append(("sql", str(inp["sql"])))
                    if inp.get("command"):
                        out["code"].append(("python", str(inp["command"])))
            elif kind == "chart":
                spec = (blk.get("chart") or {}).get("chart_spec")
                if spec:
                    out["charts"].append(spec)
    return out


# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------

def page_portfolio():
    masthead("Site feasibility portfolio",
             "Capability read from survey PDFs, normalised to a controlled taxonomy, "
             "joined to site and study reference data",
             "Live")

    k = kpis()
    cd = coverage_detail()
    total_w = sum(SHORTLIST_WEIGHTS.values())
    kpi_row([
        ("Sites assessed", f"{int(k.SITES_ASSESSED)}<span style='font-size:1rem;color:{MUTED}'>"
                           f" of {int(k.SITES_TOTAL)}</span>",
         f"{int(k.SITES_TOTAL) - int(k.SITES_ASSESSED)} sites have not returned a survey", DEEP),
        ("Study requirements met",
         f"{int(cd.AVAILABLE)}<span style='font-size:1rem;color:%s'> of "
         f"{int(cd.REQUIRED)}</span>" % MUTED,
         f"across the {int(cd.SITES)} sites that returned a survey",
         GOOD if k.COVERAGE >= 0.8 else WARN),
        ("Requirements not met", f"{int(cd.MISSING)}",
         f"at {int(cd.SITES_WITH_GAPS)} of the {int(cd.SITES)} assessed sites", WARN),
        ("Read correctly", f"{k.MEAN_CONFIDENCE:.0%}",
         f"average confidence · {int(k.REVIEW_ITEMS)} item(s) need a human check",
         GOOD if int(k.REVIEW_ITEMS) == 0 else WARN),
    ])

    # Business users asked the obvious question first: why is coverage not 100%?
    # Answer it on the page rather than making them work it out.
    st.markdown(
        '<div class="banner b-info"><b>What "requirements met" means.</b> '
        "Every study declares the procedures and equipment a site must have. Those "
        "lists differ by study, so the same site is measured against a different bar "
        f"depending on what is being asked of it. Across the {int(cd.SITES)} sites that "
        f"returned a survey the studies need {int(cd.REQUIRED)} things in total, and "
        f"{int(cd.AVAILABLE)} are in place. The remaining {int(cd.MISSING)} are either "
        "something the site told us it does not have, or something the study needs and "
        "the survey never mentioned. Neither rules a site out - both are worth a phone "
        "call.</div>",
        unsafe_allow_html=True)

    if abs(total_w - 1.0) > 0.001:
        st.markdown(
            f'<div class="banner b-warn">SHORTLIST_WEIGHTS sum to {total_w:.2f}, not 1.00. '
            "The ranking still works but the scores are no longer on a 0-1 scale.</div>",
            unsafe_allow_html=True)

    sl = shortlist(SHORTLIST_WEIGHTS)

    # Deliberately few columns. An earlier version carried institution name, therapy
    # area, startup weeks and a separate location column, and at that width the score -
    # the whole point of the table - was the first thing to clip.
    rows = []
    for i, r in enumerate(sl.itertuples(), 1):
        met = (f'<span class="tag t-can">{int(r.ITEMS_AVAILABLE)} of '
               f'{int(r.ITEMS_REQUIRED)}</span>' if r.ITEMS_MISSING == 0
               else f'<span class="tag t-cannot">{int(r.ITEMS_AVAILABLE)} of '
                    f'{int(r.ITEMS_REQUIRED)}</span>')
        rows.append(
            f'<tr><td class="rank">{i}</td>'
            f'<td><div class="site">{r.SITE_ID}</div>'
            f'<div class="where">{r.CITY}, {r.COUNTRY}</div></td>'
            f'<td>{met}</td>'
            f'<td class="num">{int(r.PLANNED_ENROLLED)}</td>'
            # title= puts the arithmetic for this row one hover away. The score is
            # the whole point of the table, so it should be checkable in place.
            f'<td class="num" style="font-weight:650;font-size:.92rem" '
            f'title="requirements {r.PART_REQUIREMENTS:.2f} x '
            f'{SHORTLIST_WEIGHTS["capability_coverage"]:.0%} + '
            f'enrolment {r.PART_ENROLMENT:.2f} x '
            f'{SHORTLIST_WEIGHTS["planned_enrolment"]:.0%} + '
            f'speed {r.PART_SPEED:.2f} x '
            f'{SHORTLIST_WEIGHTS["startup_speed"]:.0%} = {r.SCORE:.2f}">'
            f'{r.SCORE:.2f}</td></tr>')
    # The score is a composite we invented for this portfolio, not a domain
    # standard. It goes ABOVE the table it ranks: a reader meets the definition
    # before the number, rather than scrolling to find out what they just read.
    st.markdown(
        f'<div class="card"><h3>How the score is calculated</h3>'
        f'<div class="note">Every site is scored out of 1.00 on three things. Each '
        f'is put on a 0-1 scale across the assessed sites first, so they can be '
        f'weighted against each other, then the three are added up.</div>'
        '<table class="grid"><thead><tr><th>Factor</th>'
        '<th style="text-align:right">Weight</th>'
        '<th>What it rewards</th><th>Where the number comes from</th></tr></thead>'
        '<tbody>'
        f'<tr><td>Study requirements met</td><td class="num">'
        f'{SHORTLIST_WEIGHTS["capability_coverage"]:.0%}</td>'
        '<td>A site that can already do what the protocol needs</td>'
        '<td>Read from the survey PDFs</td></tr>'
        f'<tr><td>Planned enrolment</td><td class="num">'
        f'{SHORTLIST_WEIGHTS["planned_enrolment"]:.0%}</td>'
        '<td>A site that expects to recruit more patients</td>'
        '<td>Enrolment reference data</td></tr>'
        f'<tr><td>Startup speed</td><td class="num">'
        f'{SHORTLIST_WEIGHTS["startup_speed"]:.0%}</td>'
        '<td>A site that can be open sooner</td>'
        '<td>Enrolment reference data</td></tr>'
        '</tbody></table>'
        '<div class="note" style="margin-top:.7rem">So a score of 1.00 would be a '
        'site that meets every requirement, plans the most patients of anyone, and '
        'opens fastest. Nobody is 1.00. These weights are a judgement about what '
        'this programme values, not an industry standard - weight requirements more '
        'heavily and the site that meets every requirement rises to the top; weight '
        'enrolment and the highest-volume site wins despite its gap. The ranking '
        'should be arguable, and visible enough to argue with.</div>'
        '</div>',
        unsafe_allow_html=True)

    st.markdown(
        f'<div class="card"><h3>Which sites to approach first</h3>'
        '<div class="note">Ranked by the score above, out of 1.00. '
        'Hover a score to see the three parts that made it.</div>'
        '<table class="grid"><thead><tr><th></th><th>Site</th>'
        '<th>Study requirements met</th>'
        '<th style="text-align:right">Patients planned</th>'
        '<th style="text-align:right">Score / 1.00</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>',
        unsafe_allow_html=True)

    left, right = st.columns([1, 1], gap="medium")

    with left:
        st.markdown('<div class="sec">Requirements met against enrolment</div>',
                    unsafe_allow_html=True)
        st.caption("Upper left is the risk quadrant: enrolment ambition without the "
                   "capability to deliver it.")
        d = sl.copy()
        d["Risk"] = ((d.PLANNED_ENROLLED >= d.PLANNED_ENROLLED.median())
                     & (d.COVERAGE < d.COVERAGE.median())).map({True: "At risk", False: "OK"})
        pts = alt.Chart(d).mark_circle(size=190, opacity=.88).encode(
            x=alt.X("COVERAGE:Q", title="Study requirements met",
                    axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0.6, 1.03])),
            y=alt.Y("PLANNED_ENROLLED:Q", title="Planned enrolment",
                    scale=alt.Scale(nice=True, zero=False)),
            color=alt.Color("Risk:N", scale=alt.Scale(domain=["OK", "At risk"],
                                                      range=[BRAND, BAD]),
                            legend=alt.Legend(orient="bottom", title=None)),
            tooltip=[alt.Tooltip("SITE_ID", title="Site"),
                     alt.Tooltip("INSTITUTION_NAME", title="Institution"),
                     alt.Tooltip("CITY", title="City"),
                     alt.Tooltip("COVERAGE:Q", format=".1%", title="Requirements met"),
                     alt.Tooltip("PLANNED_ENROLLED", title="Planned"),
                     alt.Tooltip("ITEMS_MISSING", title="Gaps")],
        )
        # Only the at-risk points are labelled. Labelling all ten made them overprint.
        labels = alt.Chart(d[d.Risk == "At risk"]).mark_text(
            dx=13, dy=-10, fontSize=9, fontWeight=600, color=BAD, align="left"
        ).encode(x="COVERAGE:Q", y="PLANNED_ENROLLED:Q", text="SITE_ID:N")
        st.altair_chart(
            (pts + labels).properties(height=290).configure_view(strokeWidth=0)
            .configure_axis(labelColor=MUTED, titleColor=MUTED, labelFontSize=10,
                            titleFontSize=10, gridColor="#EDF2F7",
                            domainColor=LINE, tickColor=LINE, titleFontWeight="normal")
            .configure_legend(labelColor=MUTED, labelFontSize=10, symbolType="circle"),
            use_container_width=True)

    with right:
        hm = heat()
        if not hm.empty:
            st.markdown('<div class="sec">Requirements met by category</div>',
                        unsafe_allow_html=True)
            st.caption("Two bars per site. Equipment gaps are usually cheaper to "
                       "close than procedure gaps.")
            # This was a heatmap, and a heatmap was the wrong choice: reading a
            # percentage off a shade means going back to the legend for every
            # cell, and with only two categories there is no pattern that needs
            # a matrix. Paired bars put both numbers on a common axis, so the
            # comparison is a length rather than a colour match.
            order = (hm.groupby("SITE_ID").COVERAGE.mean()
                     .sort_values(ascending=False).index.tolist())
            # Give each site a 46px band for its pair of bars rather than dividing
            # a fixed height between them. Ten sites at 9px with no gap read as one
            # striped block; the eye needs the white space to see them as pairs.
            band = 46
            # The x domain runs past 100% on purpose. With a domain of exactly
            # [0, 1] the label on a full bar is drawn at the plot edge and clipped,
            # which is why "100%" was appearing as "100".
            bars = alt.Chart(hm).mark_bar(cornerRadiusEnd=2, height=13).encode(
                x=alt.X("COVERAGE:Q", title="Requirements met",
                        axis=alt.Axis(format="%", values=[0, .25, .5, .75, 1]),
                        scale=alt.Scale(domain=[0, 1.14], nice=False)),
                y=alt.Y("SITE_ID:N", title=None, sort=order,
                        axis=alt.Axis(labelOverlap=False, labelFontSize=11,
                                      labelPadding=6, ticks=False,
                                      labelFontWeight=600)),
                yOffset=alt.YOffset("ITEM_CATEGORY:N", sort="ascending"),
                color=alt.Color("ITEM_CATEGORY:N", title=None,
                                scale=alt.Scale(domain=["EQUIPMENT", "PROCEDURE"],
                                                range=[BRAND, DEEP]),
                                legend=alt.Legend(orient="top", offset=6,
                                                  symbolSize=90)),
                tooltip=[alt.Tooltip("SITE_ID", title="Site"),
                         alt.Tooltip("ITEM_CATEGORY", title="Category"),
                         alt.Tooltip("COVERAGE:Q", format=".0%", title="Requirements met")],
            )
            # Labels on the bar ends, both series in the same grey. Colouring them
            # to match the bar made the light blue ones almost invisible.
            text = bars.mark_text(align="left", dx=5, fontSize=10,
                                  color="#475569", fontWeight=600
                                  ).encode(text=alt.Text("COVERAGE:Q", format=".0%"))
            st.altair_chart(
                (bars + text).properties(height=band * hm.SITE_ID.nunique())
                .configure_view(strokeWidth=0)
                .configure_axis(labelColor=MUTED, titleColor=MUTED, labelFontSize=10,
                                titleFontSize=10, gridColor="#EDF2F7",
                                domainColor=LINE, tickColor=LINE,
                                titleFontWeight="normal")
                .configure_legend(labelColor=MUTED, labelFontSize=11,
                                  symbolType="square"),
                use_container_width=True)


def page_documents():
    masthead("Document review",
             "The survey on the left, what the pipeline read out of it on the right. "
             "Check one against the other.",
             "Evidence")

    # This page reads the parsed pages, built in step 3.
    if not have("EXTRACTED", "PAGES"):
        needs_step("step 3", "The parsed survey text")

    docs = documents()
    labels = {r.DOCUMENT_ID: f"{r.SITE_ID} - {r.INSTITUTION_NAME}, {r.CITY} ({r.STUDY_CODE})"
              for r in docs.itertuples()}
    doc_id = st.selectbox("Survey", list(labels), format_func=lambda d: labels[d],
                          label_visibility="collapsed")
    meta = docs[docs.DOCUMENT_ID == doc_id].iloc[0]
    items = doc_items(doc_id)
    pages = doc_pages(doc_id)

    req = items[items.IS_REQUIRED_FOR_STUDY]
    can = int((req.CAPABILITY == "CAN").sum())
    kpi_row([
        ("Study", f"{meta.STUDY_CODE}", f"{meta.DRUG_NAME} · {meta.THERAPY_AREA}", DEEP),
        ("Required items met", f"{can} of {len(req)}",
         "everything this study needs" if can == len(req) else f"{len(req) - can} gap(s)",
         GOOD if can == len(req) else WARN),
        ("Items read in total", f"{len(items)}",
         f"from {int(meta.PAGES)} page(s) of survey", DEEP),
        ("Names corrected", f"{int(items.WAS_NORMALISED.sum())}",
         "site wording matched to the standard list", DEEP),
    ])

    st.markdown(
        '<div class="banner b-info"><b>This is the page that builds trust.</b> '
        "The signed survey on the left, exactly as the site returned it. What the "
        "pipeline read out of it on the right. No accuracy metric is as convincing "
        "as seeing the ticked box and the row it produced, side by side, on a "
        "document you recognise.</div>",
        unsafe_allow_html=True)

    left, right = st.columns([1, 1], gap="medium")

    with left:
        st.markdown('<div class="sec">The survey as returned</div>',
                    unsafe_allow_html=True)
        st.caption("The original signed PDF, served straight from the Snowflake "
                   "stage. Nothing has been retyped or transcribed.")
        try:
            # Images, not st.pdf - see pdf_page_images for why. The scrolling
            # container keeps the two columns the same height however many
            # pages the survey runs to.
            imgs = pdf_page_images(doc_id)
            with st.container(height=600, border=True):
                for n, png in enumerate(imgs, start=1):
                    if len(imgs) > 1:
                        st.markdown(
                            f'<div class="note" style="margin:.2rem 0 .3rem">'
                            f'Page {n} of {len(imgs)}</div>',
                            unsafe_allow_html=True)
                    st.image(png, use_container_width=True)
            a, b = st.columns(2)
            a.link_button("Open the original in a new tab", pdf_url(doc_id),
                          use_container_width=True)
            b.download_button("Download the PDF", pdf_bytes(doc_id),
                              file_name=f"{doc_id}.pdf",
                              mime="application/pdf", use_container_width=True,
                              key=f"dl_{doc_id}")
        except Exception as exc:                                # noqa: BLE001
            st.warning(f"Could not render the survey inline: {exc}")
            st.link_button("Open the PDF in a new tab", pdf_url(doc_id),
                           use_container_width=True)

        with st.expander("What the parser read as text"):
            st.caption("AI_PARSE_DOCUMENT in LAYOUT mode. Tick boxes come back "
                       "as characters, so you can see exactly what was marked.")
            for r in pages.itertuples():
                if int(meta.PAGES) > 1:
                    st.markdown(
                        f'<div class="note" style="margin:.5rem 0 .25rem">'
                        f'Page {r.PAGE_INDEX + 1} of {int(meta.PAGES)}</div>',
                        unsafe_allow_html=True)
                safe = (str(r.PAGE_TEXT).replace("&", "&amp;")
                        .replace("<", "&lt;").replace(">", "&gt;"))
                st.markdown(f'<div class="doc">{safe}</div>',
                            unsafe_allow_html=True)

    with right:
        st.markdown('<div class="sec">What AI_EXTRACT read out of it</div>',
                    unsafe_allow_html=True)
        st.caption("Only the items this study requires. Check each one against the "
                   "PDF on the left - including the ticked boxes.")

        # He gets asked this in every session, so the answer lives on the page
        # next to the numbers it explains. One line visible, the detail folded
        # away so it does not push the table down.
        st.markdown(
            '<div class="note" style="margin:-.2rem 0 .5rem">'
            '<b>Confidence</b> is the model\'s own assessment of each extracted '
            'field, calibrated against measured accuracy - use it to route '
            f'anything below {MIN_CONFIDENCE:.0%} to a human.</div>',
            unsafe_allow_html=True)
        with st.expander("How the confidence score is produced"):
            st.markdown(
                "- The model returns a score alongside each extracted value, "
                "in the same pass as the answer itself.\n"
                "- Models tend to be overconfident, so the raw value is "
                "calibrated against measured accuracy before it is returned. "
                "The score tracks real hit rates.\n"
                "- It is scored per field, not per document, so one weak value "
                "does not put the whole survey in doubt.\n"
                f"- Use it to route work: here anything under {MIN_CONFIDENCE:.0%} "
                "is flagged for a human. Set your own cut by labelling a sample "
                "of your documents and measuring accuracy at a few thresholds.")

        derived = req[req.CAPABILITY_SOURCE == "DERIVED_ABSENT"]
        if not derived.empty:
            st.markdown(
                f'<div class="banner b-info"><b>{len(derived)} gap(s) inferred.</b> '
                "The study needs the item and the survey never mentioned it, rather "
                "than the site ticking no. Real gaps, but worth confirming.</div>",
                unsafe_allow_html=True)
        flagged = items[items.NEEDS_REVIEW]
        if not flagged.empty:
            st.markdown(
                f'<div class="banner b-warn"><b>{len(flagged)} item(s) need a human '
                "check.</b> Confidence was too low to rely on.</div>",
                unsafe_allow_html=True)

        rows = []
        for r in req.itertuples():
            if r.CAPABILITY == "CAN":
                tag = '<span class="tag t-can">available</span>'
            elif r.CAPABILITY_SOURCE == "DERIVED_ABSENT":
                tag = '<span class="tag t-inferred">inferred gap</span>'
            else:
                tag = '<span class="tag t-cannot">not available</span>'
            written = (f'<div class="where">survey wrote: {r.EXTRACTED_AS}</div>'
                       if r.WAS_NORMALISED and r.EXTRACTED_AS else "")
            conf = f"{r.CONFIDENCE:.0%}" if pd.notna(r.CONFIDENCE) else "-"
            rows.append(f'<tr><td><div>{r.ITEM}</div>{written}</td>'
                        f'<td>{tag}</td><td class="num">{conf}</td></tr>')
        st.markdown(
            '<div class="card"><table class="grid"><thead><tr><th>Item</th>'
            '<th>Status</th><th style="text-align:right">Confidence</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>',
            unsafe_allow_html=True)

        st.markdown('<div class="sec" style="margin-top:1rem">Plain English summary</div>',
                    unsafe_allow_html=True)
        key = f"explain_{doc_id}"
        if st.button("Summarise this survey", use_container_width=True,
                     key=f"btn_{doc_id}"):
            with st.spinner("Reading the survey..."):
                try:
                    st.session_state[key] = explain_document(doc_id)
                except Exception as exc:                        # noqa: BLE001
                    st.session_state[key] = f"Could not generate: {exc}"
        if st.session_state.get(key):
            st.markdown(f'<div class="card">{st.session_state[key]}</div>',
                        unsafe_allow_html=True)


def page_site():
    masthead("Site detail", "Every capability, with its evidence and confidence", "Live")
    sites = assessed_sites()
    labels = {r.SITE_ID: f"{r.SITE_ID} - {r.INSTITUTION_NAME}, {r.CITY}, {r.COUNTRY}"
              for r in sites.itertuples()}
    site_id = st.selectbox("Site", list(labels), format_func=lambda s: labels[s],
                           label_visibility="collapsed")
    h = site_header(site_id)
    items = site_items(site_id)

    kpi_row([
        ("Study requirements met",
         f"{int(h.ITEMS_AVAILABLE)}<span style='font-size:1rem;color:%s'> of "
         f"{int(h.ITEMS_REQUIRED)}</span>" % MUTED,
         "everything this study needs" if int(h.ITEMS_AVAILABLE) == int(h.ITEMS_REQUIRED)
         else f"{int(h.ITEMS_REQUIRED) - int(h.ITEMS_AVAILABLE)} not met",
         GOOD if h.COVERAGE >= 0.85 else (WARN if h.COVERAGE >= 0.7 else BAD)),
        ("Planned enrolment", f"{int(h.PLANNED_ENROLLED)}",
         f"{int(h.PLANNED_SCREENED)} screened, {int(h.PATIENTS_SEEN_PER_YEAR)} seen per year", DEEP),
        ("Startup", f"{int(h.STARTUP_WEEKS)}w", "Protocol to first patient in", DEEP),
        ("Competing studies", f"{int(h.COMPETING_STUDIES)}",
         f"{h.INSTITUTION_TYPE} site in {h.REGION}", DEEP),
    ])

    # Worth being explicit, and worth being the headline rather than a footnote.
    # Only the first tile came out of the PDF; the other three are reference data
    # the programme already held. Neither half answers the feasibility question on
    # its own - the capability answers were stuck in documents and the numbers were
    # already in the warehouse, and the shortlist only exists because they are now
    # in the same query. Blurring that line oversells the extraction and undersells
    # the join, which is the actual point of the lab.
    st.markdown(
        f'<div class="banner b-info" style="border-left:3px solid {BRAND}">'
        '<b>Two halves of the same question.</b> '
        '<b>Study requirements met</b> was read out of this site\'s survey PDF by '
        'AI_EXTRACT - unstructured, and until now unqueryable. '
        '<b>Planned enrolment, startup and competing studies</b> are structured '
        'reference data that was already in the warehouse. Neither half can rank a '
        'site on its own. The shortlist exists because both are now in one query.'
        '</div>',
        unsafe_allow_html=True)

    st.markdown(
        f'<div class="card"><h3>{h.INSTITUTION_NAME}</h3><div class="note">'
        f'{h.CITY}, {h.COUNTRY} &nbsp;·&nbsp; {h.STUDY_CODE} {h.DRUG_NAME} &nbsp;·&nbsp; '
        f'Phase {h.PHASE} &nbsp;·&nbsp; {h.INDICATION}</div></div>',
        unsafe_allow_html=True)

    rows = []
    for r in items[items.IS_REQUIRED_FOR_STUDY].itertuples():
        if r.CAPABILITY == "CAN":
            tag = '<span class="tag t-can">available</span>'
        elif r.CAPABILITY_SOURCE == "DERIVED_ABSENT":
            tag = '<span class="tag t-inferred">inferred gap</span>'
        else:
            tag = '<span class="tag t-cannot">not available</span>'
        written = (f'<div class="where">survey wrote: {r.EXTRACTED_AS}</div>'
                   if r.WAS_NORMALISED and r.EXTRACTED_AS else "")
        conf = f"{r.CONFIDENCE:.0%}" if pd.notna(r.CONFIDENCE) else "-"
        rows.append(f'<tr><td><div>{r.ITEM}</div>{written}</td>'
                    f'<td><span class="tag t-neutral">{r.ITEM_CATEGORY.lower()}</span></td>'
                    f'<td>{tag}</td><td class="num">{conf}</td></tr>')
    card("What this study needs from this site",
         "Only the items this study requires count towards the score. Other "
         "capabilities the site has are recorded but do not affect it.",
         '<table class="grid"><thead><tr><th>Item</th><th>Category</th><th>Status</th>'
         '<th style="text-align:right">Confidence</th></tr></thead>'
         f'<tbody>{"".join(rows)}</tbody></table>')

    norm = items[items.WAS_NORMALISED]
    if not norm.empty:
        with st.expander(f"{len(norm)} item name(s) repaired by similarity matching"):
            st.caption("Site staff type free text, so survey wording rarely matches a "
                       "controlled vocabulary. Resolved by embedding cosine similarity "
                       "at a 0.85 threshold.")
            nrows = "".join(
                f'<tr><td>{r.EXTRACTED_AS}</td><td>{r.ITEM}</td>'
                f'<td class="num">{r.NAME_SIMILARITY:.3f}</td></tr>'
                for r in norm.itertuples())
            st.markdown(
                '<table class="grid"><thead><tr><th>Survey wording</th>'
                '<th>Resolved to</th>'
                '<th style="text-align:right">Similarity</th></tr></thead>'
                f'<tbody>{nrows}</tbody></table>',
                unsafe_allow_html=True)


def page_ask():
    masthead("Ask", "Cortex Agent with three tools: structured analytics, document "
                    "search, and sandboxed Python", "Agent")

    # The agent's search tool points at SURVEY_SEARCH, built in step 3. Without it
    # the agent still starts and then fails mid-answer with a 404 naming the
    # service, which reads like a broken agent. Say so up front instead.
    if not have_search("SURVEY_SEARCH"):
        needs_step("step 3", "The agent's document search service")

    samples = [
        "Which sites can perform Cardiac MRI, and which cannot?",
        "Which sites raised concerns in their survey comments?",
        "Plot study requirements met against planned enrolment and highlight any site that is high enrolment but low on requirements.",
        "Shortlist the best three sites for the ATTR-CM study and explain the trade-offs.",
    ]
    # Two by two rather than four across. Four columns forced every label down
    # to about thirty characters, so the buttons all read the same and the real
    # question only appeared in a tooltip that overflowed the card.
    st.markdown('<div class="sec">Try one of these</div>', unsafe_allow_html=True)
    picked = None
    for row in (samples[:2], samples[2:]):
        for col, s in zip(st.columns(len(row)), row):
            short = s if len(s) <= 62 else s[:59].rstrip(" ,") + "..."
            if col.button(short, help=s, use_container_width=True, key=f"s_{s[:20]}"):
                picked = s

    if "chat" not in st.session_state:
        st.session_state.chat = []

    # Material icons rather than Streamlit's default avatars, which are two
    # coloured squares and are the single thing that makes a chat look unfinished.
    AVATAR = {"user": ":material/person:", "assistant": ":material/graphic_eq:"}

    def render(turn):
        st.markdown(turn["content"])
        for spec in turn.get("charts", []):
            try:
                st.vega_lite_chart(json.loads(spec), use_container_width=True)
            except Exception:                                       # noqa: BLE001
                st.caption("Chart could not be rendered.")
        # The working is worth showing but is not the answer, so it sits under it
        # as pills and folded panels rather than as more prose.
        if turn.get("tools"):
            st.markdown(
                '<div style="margin:.5rem 0 .2rem">' + "".join(
                    f'<span class="pill">{t}</span>'
                    for t in dict.fromkeys(turn["tools"])) + "</div>",
                unsafe_allow_html=True)
        if turn.get("thinking"):
            with st.expander("How it worked this out"):
                st.markdown(turn["thinking"])
        for lang, src in turn.get("code", []):
            with st.expander(f"The {lang.upper()} it ran"):
                st.code(src, language=lang)

    for turn in st.session_state.chat:
        with st.chat_message(turn["role"], avatar=AVATAR[turn["role"]]):
            render(turn) if turn["role"] == "assistant" else st.markdown(turn["content"])

    question = st.chat_input("Ask about site feasibility") or picked
    if not question:
        return

    st.session_state.chat.append({"role": "user", "content": question})
    with st.chat_message("user", avatar=AVATAR["user"]):
        st.markdown(question)

    with st.chat_message("assistant", avatar=AVATAR["assistant"]):
        with st.spinner("Thinking, querying and running code..."):
            r = ask_agent(question)
        if r["error"]:
            # Report what Snowflake said rather than guessing at a cause. The two
            # real causes look nothing alike: a missing search service names the
            # service, and a permissions problem names the role.
            st.markdown(
                '<div class="banner b-warn"><b>The agent call failed.</b> The other three '
                "pages are pure SQL and unaffected.<br><br>"
                f"<code>{r['error']}</code></div>", unsafe_allow_html=True)
            st.session_state.chat.pop()
            return
        turn = {"role": "assistant", "content": r["answer"] or "No answer returned.",
                "charts": r["charts"], "thinking": r["thinking"],
                "code": r["code"], "tools": r["tools"]}
        render(turn)
        st.session_state.chat.append(turn)


PAGES = {
    "Portfolio": page_portfolio,
    "Document review": page_documents,
    "Site detail": page_site,
    "Ask": page_ask,
}

if "page" not in st.session_state:
    st.session_state.page = "Portfolio"


def _go(name: str):
    """Set the page from a button callback.

    This has to be a callback rather than `if st.button(...)`. Callbacks run before
    the script body on the rerun, so the page function below sees the new value. With
    the inline form, state is set after the nav has already rendered and the content
    lags a click behind.
    """
    st.session_state.page = name


st.markdown('<div class="nav-anchor"></div>', unsafe_allow_html=True)
nav = st.columns([1.1, 1.5, 1.2, 0.8, 4.4], gap="small")
for col, name in zip(nav, PAGES):
    col.button(name, use_container_width=True, key=f"nav_{name}",
               on_click=_go, args=(name,),
               type="primary" if st.session_state.page == name else "secondary")

PAGES[st.session_state.page]()

