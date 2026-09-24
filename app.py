import streamlit as st
import pandas as pd
import mysql.connector
import plotly.express as px
from google import genai
from google.genai import errors as genai_errors
import re

# -----------------------------
# Page Configuration
# -----------------------------
st.set_page_config(
    page_title="Supply Chain Database AI Analyst",
    page_icon="📊",
    layout="wide"
)

st.title("📊 Supply Chain Database AI Analyst")
st.caption("Ask natural-language questions about your supply chain data.")

# -----------------------------
# Session State Initialization
# -----------------------------
for key, default in [
    ("df", None),
    ("conn", None),
    ("db_config", None),
    ("table_schema", None),
    ("table_defs", None),
    ("relationships", None),
    ("chat_history", []),
]:
    if key not in st.session_state:
        st.session_state[key] = default

DATABASE_NAME = "supply_chain_db"

# -----------------------------
# Helper Functions: DB Discovery
# -----------------------------
def get_all_tables(conn, database_name: str) -> list[str]:
    query = """
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
    """
    df = pd.read_sql_query(query, conn, params=(database_name,))
    return df["TABLE_NAME"].tolist()


def get_columns_metadata(conn, database_name: str) -> pd.DataFrame:
    query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """
    return pd.read_sql_query(query, conn, params=(database_name,))


def get_foreign_keys(conn, database_name: str) -> pd.DataFrame:
    query = """
        SELECT
            TABLE_NAME,
            COLUMN_NAME,
            REFERENCED_TABLE_NAME,
            REFERENCED_COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY TABLE_NAME, COLUMN_NAME
    """
    return pd.read_sql_query(query, conn, params=(database_name,))


def build_table_definitions(columns_df: pd.DataFrame) -> str:
    lines = []
    for table_name, group in columns_df.groupby("TABLE_NAME", sort=False):
        cols = [
            f"{row.COLUMN_NAME}{' [PK]' if row.COLUMN_KEY == 'PRI' else ''}"
            for row in group.itertuples(index=False)
        ]
        lines.append(f"- {table_name}({', '.join(cols)})")
    return "\n".join(lines)


def build_relationships(fk_df: pd.DataFrame) -> str:
    if not fk_df.empty:
        lines = [
            f"- {row.TABLE_NAME}.{row.COLUMN_NAME} = {row.REFERENCED_TABLE_NAME}.{row.REFERENCED_COLUMN_NAME}"
            for row in fk_df.itertuples(index=False)
        ]
        return "\n".join(lines)
    # Default explicit joins for supply_chain_db if FK constraints were not added in MySQL
    return (
        "- supply_chain_records.product_id = products.product_id\n"
        "- supply_chain_records.region_id = regions.region_id"
    )

# -----------------------------
# Robust SQL Cleaning & Extraction
# -----------------------------
def clean_and_extract_sql(text: str) -> str:
    """Extracts only the actual SQL statement even if Gemini adds conversational filler."""
    # Remove markdown code block fences
    text = re.sub(r"```sql\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*", "", text)
    
    # Remove SQL single-line comments
    text = re.sub(r"--.*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    # Search for the starting keyword (SELECT, WITH, SHOW, DESCRIBE, EXPLAIN)
    match = re.search(r"\b(SELECT|WITH|SHOW|DESCRIBE|EXPLAIN)\b[\s\S]*", text, flags=re.IGNORECASE)
    if match:
        sql = match.group(0).strip()
        # If there are multiple queries or text after a semicolon, keep only the first query
        if ";" in sql:
            sql = sql.split(";")[0] + ";"
        return sql
    return text.strip()


def is_read_only_sql(sql: str) -> bool:
    clean = sql.strip().lower()
    # Must start with safe read-only commands
    if not clean.startswith(("select", "with", "show", "describe", "explain")):
        return False
    # Forbidden keywords
    forbidden = [
        "insert ", "update ", "delete ", "drop ", "alter ",
        "truncate ", "create ", "replace ", "grant ", "revoke "
    ]
    return not any(f in clean for f in forbidden)


def get_active_connection():
    """Ensures MySQL connection is still alive, reconnecting if timed out."""
    conn = st.session_state.conn
    if conn is None:
        return None
    try:
        conn.ping(reconnect=True, attempts=3, delay=1)
        return conn
    except Exception:
        # Re-establish using saved config
        if st.session_state.db_config:
            new_conn = mysql.connector.connect(**st.session_state.db_config)
            st.session_state.conn = new_conn
            return new_conn
    return None


def auto_chart(df: pd.DataFrame):
    if df.shape[1] == 2 and pd.api.types.is_numeric_dtype(df.iloc[:, 1]):
        return px.bar(df, x=df.columns[0], y=df.columns[1], title=f"{df.columns[1]} by {df.columns[0]}")
    return None

# -----------------------------
# AI Prompting & Self-Healing
# -----------------------------
def generate_sql(client, model_name, question, schema, table_defs, relationships, history, error_feedback=None):
    error_instruction = ""
    if error_feedback:
        error_instruction = f"""
IMPORTANT: Your previous SQL query failed with this error:
"{error_feedback['error']}"
Query that failed:
{error_feedback['sql']}
Please fix this issue in your new SQL query.
"""

    prompt = f"""
You are a MySQL expert specializing in supply chain analytics.

Database: {DATABASE_NAME}
Tables:
{table_defs}

Relationships:
{relationships}

Column Details:
{schema}

{error_instruction}

User question:
{question}

Previous questions:
{history}

Rules:
1. Generate ONLY ONE valid read-only MySQL SQL statement.
2. When answering queries requiring product names or regions, join 'supply_chain_records' with 'products' and 'regions'.
3. Follow MySQL ONLY_FULL_GROUP_BY standards: all non-aggregated SELECT columns must appear in the GROUP BY clause.
4. Output RAW SQL ONLY. Do not wrap in markdown or backticks. Do not include greetings or explanations.
"""
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
    )
    return clean_and_extract_sql(response.text)


def explain_result(client, model_name, question, sql, result_df):
    try:
        prompt = f"""
User question:
{question}

SQL executed:
{sql}

Result (first 20 rows):
{result_df.head(20).to_string(index=False)}

Explain this data result clearly and concisely for a business user. Highlight key insights.
"""
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        return f"Results retrieved successfully. (AI Explanation skipped: {e})"


# -----------------------------
# UI Layout
# -----------------------------
left, right = st.columns([1, 2])

# =============================
# LEFT PANE: Configuration
# =============================
with left:
    st.subheader("🔧 Setup")

    api_key = st.text_input("Gemini API Key", type="password")
    
    model_choice = st.selectbox(
        "Gemini Model",
        options=[
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
            "gemini-3.8-flash"
        ],
        index=0,
        help="Select a Gemini 3.1+ model for SQL generation and analysis."
    )

    st.markdown("---")
    host = st.text_input("MySQL host", value="localhost")
    port = st.number_input("MySQL port", min_value=1, max_value=65535, value=3306)
    user = st.text_input("MySQL username")
    password = st.text_input("MySQL password", type="password")

    if st.button("Connect to supply_chain_db", type="primary"):
        try:
            db_config = {
                "host": host,
                "port": int(port),
                "user": user,
                "password": password,
                "database": DATABASE_NAME,
            }
            conn = mysql.connector.connect(**db_config)

            table_names = get_all_tables(conn, DATABASE_NAME)
            if not table_names:
                raise ValueError(f"No tables found in database '{DATABASE_NAME}'.")

            schema_df = get_columns_metadata(conn, DATABASE_NAME)
            fk_df = get_foreign_keys(conn, DATABASE_NAME)

            table_defs = build_table_definitions(schema_df)
            relationships = build_relationships(fk_df)

            schema = "\n".join(
                f"- {row.TABLE_NAME}.{row.COLUMN_NAME}: {row.COLUMN_TYPE}"
                for row in schema_df.itertuples(index=False)
            )

            preview_table = "supply_chain_records" if "supply_chain_records" in table_names else table_names[0]
            preview_df = pd.read_sql_query(f"SELECT * FROM `{preview_table}` LIMIT 10", conn)

            st.session_state.df = preview_df
            st.session_state.conn = conn
            st.session_state.db_config = db_config
            st.session_state.table_schema = schema
            st.session_state.table_defs = table_defs
            st.session_state.relationships = relationships

            st.success(f"Connected! Discovered: {', '.join(table_names)}")
            st.dataframe(preview_df.head(5), use_container_width=True)

        except Exception as e:
            st.error(f"Database connection failed: {e}")

# =============================
# RIGHT PANE: Chatbot & Analysis
# =============================
with right:
    st.subheader("💬 Data Chatbot")

    if not api_key:
        st.info("👈 Enter your Gemini API key in the left sidebar to start.")
        st.stop()

    if st.session_state.conn is None or st.session_state.table_schema is None:
        st.info("👈 Connect to your MySQL database in the left sidebar first.")
        st.stop()

    question = st.text_input("Ask a question about your supply chain data:", placeholder="e.g. Which product category has the highest units sold in each region?")

    if st.button("Analyze", type="primary") and question:
        client = genai.Client(api_key=api_key)
        conn = get_active_connection()

        if conn is None:
            st.error("Lost connection to MySQL. Please click 'Connect to supply_chain_db' again.")
            st.stop()

        with st.spinner("Analyzing data..."):
            schema = st.session_state.table_schema
            table_defs = st.session_state.table_defs
            relationships = st.session_state.relationships
            history = st.session_state.chat_history[-3:]

            sql = None
            result_df = None
            error_feedback = None

            # Attempt execution with 1 automatic retry if SQL fails
            for attempt in range(2):
                try:
                    sql = generate_sql(
                        client, model_choice, question, schema,
                        table_defs, relationships, history, error_feedback
                    )

                    if not is_read_only_sql(sql):
                        raise ValueError(f"Generated query was flagged as unsafe or not a valid SELECT statement:\n`{sql}`")

                    # Execute query
                    result_df = pd.read_sql_query(sql, conn)
                    break  # Success!

                except (mysql.connector.Error, pd.errors.DatabaseError, ValueError) as db_err:
                    if attempt == 0:
                        # Feed the error back to Gemini to self-heal
                        error_feedback = {"sql": sql, "error": str(db_err)}
                        continue
                    else:
                        st.error(f"❌ SQL Execution Failed: {db_err}")
                        if sql:
                            st.code(sql, language="sql")
                        st.stop()

                except genai_errors.APIError as api_err:
                    st.error(f"❌ Gemini API Error: {api_err}. (Try switching the Gemini Model in the left sidebar).")
                    st.stop()

                except Exception as ex:
                    st.error(f"❌ Unexpected Error: {ex}")
                    st.stop()

            # If results were successfully retrieved
            if result_df is not None:
                st.session_state.chat_history.append(question)
                st.session_state.chat_history = st.session_state.chat_history[-3:]

                # 1. Show SQL
                st.markdown("### 🧾 SQL Generated")
                st.code(sql, language="sql")

                # 2. Show Table
                st.markdown(f"### 📊 Result ({len(result_df)} rows)")
                if result_df.empty:
                    st.warning("Query returned 0 rows.")
                else:
                    st.dataframe(result_df, use_container_width=True)

                    # 3. Optional Auto Chart
                    chart = auto_chart(result_df)
                    if chart:
                        st.plotly_chart(chart, use_container_width=True)

                # 4. Show Explanation
                st.markdown("### ✍️ Explanation")
                explanation = explain_result(client, model_choice, question, sql, result_df)
                st.write(explanation)