import json
from typing import List
from openai import OpenAI
from fastapi import FastAPI
from python_dotenv import load_dotenv
import os
import uvicorn
from logger import logger



# Load the environment variables
load_dotenv()


# -------------------------------------------------------------------
# 1. METADATA CONTEXT PREPARATION
# -------------------------------------------------------------------

# The Semantic Manifest contains governed, pre-calculated business metrics
# derived from the LedgerService querying layer.
SEMANTIC_MANIFEST = {
    "metrics": [
        {
            "name": "stock_balances", 
            "description": "Current stock balance quantities and unit symbols per product for a store", 
            "dimensions": ["store_id", "sort_order", "limit", "offset"]
        },
        {
            "name": "stock_movements", 
            "description": "Historical inventory movements per product for a store", 
            "dimensions": ["store_id", "product_id", "newest_first", "limit", "offset"]
        }
    ]
}

# The Physical Database DDL reflecting entities managed by the LedgerService.
RAW_SCHEMA_DDL = """
CREATE TABLE staff (
    id INTEGER PRIMARY KEY,
    first_name VARCHAR NOT NULL,
    last_name VARCHAR NOT NULL,
    other_names VARCHAR,
    other_details JSON
);

CREATE TABLE stores (
    id INTEGER PRIMARY KEY,
    name VARCHAR UNIQUE NOT NULL,
    created_at DATE
);

CREATE TABLE units (
    id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL,
    symbol VARCHAR
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name VARCHAR UNIQUE NOT NULL,
    sku VARCHAR UNIQUE NOT NULL,
    base_unit_id INTEGER NOT NULL REFERENCES units(id)
);

CREATE TABLE product_unit_conversions (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    unit_id INTEGER NOT NULL REFERENCES units(id),
    multiplier_to_base NUMERIC(12, 4)
);

CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    document_type VARCHAR CHECK (document_type IN ('GOODS_RECEIVED', 'DISPATCH', 'STOCK_REQUISITION', 'ISSUE_RECORDS')) NOT NULL,
    reference_no VARCHAR,
    date DATE NOT NULL,
    source_party VARCHAR,
    destination_party VARCHAR,
    remarks TEXT,
    created_at DATETIME,
    updated_at DATETIME
);

CREATE TABLE document_lines (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    entered_quantity NUMERIC(12, 4),
    entered_unit_id INTEGER NOT NULL REFERENCES units(id),
    base_quantity NUMERIC(12, 4),
    source_party VARCHAR,
    destination_party VARCHAR
);

CREATE TABLE stock_movements (
    id INTEGER PRIMARY KEY,
    store_id INTEGER REFERENCES stores(id),
    movement_date DATE NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    document_line_id INTEGER REFERENCES document_lines(id),
    movement_type VARCHAR CHECK (movement_type IN ('RECIEVE', 'ISSUE', 'STOCKTAKE')) NOT NULL,
    quantity_delta NUMERIC(12, 4),
    remarks VARCHAR,
    associated_stockmovement_id INTEGER REFERENCES stock_movements(id),
    running_balance NUMERIC(12, 4),
    target_quantity NUMERIC(12, 4),
    recorded_by INTEGER NOT NULL REFERENCES staff(id),
    created_at DATETIME,
    updated_at DATETIME,
    CONSTRAINT check_movements_delta_sign CHECK (
        (movement_type = 'RECIEVE' AND quantity_delta > 0) OR
        (movement_type = 'ISSUE' AND quantity_delta < 0) OR
        (movement_type = 'STOCKTAKE' AND quantity_delta = 0)
    )
);

CREATE TABLE stock_balances (
    store_id INTEGER REFERENCES stores(id),
    product_id INTEGER REFERENCES products(id),
    quantity NUMERIC(12, 4) DEFAULT 0,
    PRIMARY KEY (store_id, product_id)
);

CREATE TABLE intervention_logs (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    source_action_type VARCHAR CHECK (source_action_type IN ('IN_PLACE_EDIT', 'BALANCE_OVERWRITE_RECONCILE', 'INITIAL_STOCK_TAKE', 'EXPLAIN_DISCREPANCY', 'REMOVE_ASSOCIATION')) NOT NULL,
    concerned_movement_id INTEGER REFERENCES stock_movements(id),
    old_value_snapshot NUMERIC(12, 4),
    new_value_snapshot NUMERIC(12, 4) NOT NULL,
    recorded_by INTEGER NOT NULL REFERENCES staff(id),
    remarks VARCHAR,
    changed_at DATETIME
);
"""

# -------------------------------------------------------------------
# 2. TOOL DEFINITIONS FOR THE LLM
# -------------------------------------------------------------------

tools = [
    {
        "type": "function",
        "function": {
            "name": "query_semantic_layer",
            "description": (
                "Use this tool ONLY when ALL metrics requested by the user exist "
                "in the provided Semantic Manifest. Guarantees business accuracy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": "Name of the metric from the semantic manifest."
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Grouping attributes supported by the metric."
                    },
                    "filters": {
                        "type": "string",
                        "description": "Filter constraints (e.g., 'month = 2024-01')."
                    }
                },
                "required": ["metric"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_text_to_sql",
            "description": (
                "Use this fallback tool when the user requests a concept or calculation "
                "that DOES NOT exist as a metric in the Semantic Manifest."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_query": {
                        "type": "string",
                        "description": "Raw SQL query generated directly against the physical DDL."
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Explanation of why the semantic layer could not fulfill this query."
                    }
                },
                "required": ["sql_query", "reasoning"]
            }
        }
    }
]

# -------------------------------------------------------------------
# 3. ROUTING ENGINE
# -------------------------------------------------------------------

client = OpenAI( api_key=os.environ.get("GEMINI_API_KEY"), base_url="https://generativelanguage.googleapis.com/v1beta/openai/" )

def route_query(user_query: str):
    """
    Evaluates the user's natural language query against the semantic manifest
    and forces the LLM to choose between Semantic API or Text-to-SQL.
    """
    
    # System prompt injects the semantic manifest and physical DDL as context
    system_prompt = f"""
    You are an AI Data Assistant. You have access to two data retrieval pathways:
    
    1. SEMANTIC LAYER: Pre-defined, governed metrics.
       Manifest: {json.dumps(SEMANTIC_MANIFEST)}
       
    2. RAW TEXT-TO-SQL (FALLBACK): Raw database tables for custom ad-hoc queries.
       Schema DDL: {RAW_SCHEMA_DDL}

    DECISION LOGIC:
    - First, check if the required metric is listed in the SEMANTIC MANIFEST.
    - If YES -> Call `query_semantic_layer`.
    - If NO (the metric is missing or uncalculated) and the required data can be obtained from the RAW SCHEMA -> Fall back to `generate_text_to_sql`.
    - if the question can be answered without business data e.g. a salutation, then go ahead without bothering about the data
    """

    response = client.chat.completions.create(
        model="gemini-2.0-flash",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query}
        ],
        tools=tools,
        tool_choice="auto" # Allows the model to choose between text or a tool call
    )

    logger.info(f"\n[User Query]: '{user_query}'")
    logger.info(f"got back --> {json.dumps(response.choices[0].message)}")
    
    if "tool_calls" in response.choices[0].message:
        tool_call = response.choices[0].message.tool_calls[0]
        func_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)

        logger.info(f"[Selected Tool]: {func_name}")
        logger.info(f"[Tool Call Arguments]: {json.dumps(arguments, indent=2)}")


app = FastAPI()

@app.get("/{query}")
def evaluate_query(query:str):
    route_query(query)
    return {"message": "called route_query() method"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)