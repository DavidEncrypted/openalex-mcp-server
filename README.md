# OpenAlex MCP Server

This project provides a Model Context Protocol (MCP) server that allows AI agents and other MCP clients to interact with the [OpenAlex](https://openalex.org/) database, specifically focusing on scholarly works. It utilizes the [pyalex](https://github.com/J535D165/pyalex) Python library to communicate with the OpenAlex API and the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`fastmcp`) to expose functionality as MCP tools.

## Features

This server exposes the following MCP tools for interacting with OpenAlex works:

1.  **`search_works`**:
    *   **Description:** Searches for OpenAlex works based on keywords and filters. Supports boolean operators in the search query. Returns selected fields and uses cursor pagination.
    *   **Parameters:**
        *   `search_query` (string, required): Search term(s).
        *   `filters` (object, optional): Key-value pairs for filtering (e.g., `{"publication_year": 2023, "is_oa": true}`). See OpenAlex filter documentation for possible keys.
        *   `search_field` (string, optional, default: "default"): Field to search within ('title', 'abstract', 'fulltext', 'title_and_abstract', 'default' - searches title, abstract, and fulltext).
        *   `select_fields` (array of strings, optional): List of root-level fields to return (e.g., `["id", "doi", "title", "abstract"]`). Includes plaintext abstract if requested.
        *   `sort` (object, optional): Field to sort by and direction (e.g., `{"cited_by_count": "desc"}`).
        *   `per_page` (integer, optional, default: 25): Results per page (max 200).
        *   `cursor` (string, optional): Pagination cursor.
    *   **Returns:** Object with `results` (list of work objects) and `meta` (pagination info including `next_cursor`).

2.  **`get_work_details`**:
    *   **Description:** Retrieves detailed information for a specific OpenAlex work by its ID (OpenAlex ID URL, DOI URL, PMID URL, MAG ID).
    *   **Parameters:**
        *   `work_id` (string, required): Identifier for the work.
        *   `select_fields` (array of strings, optional): List of root-level fields to return. Includes plaintext abstract if requested.
    *   **Returns:** Object representing the work with selected fields, or an error object.

3.  **`get_referenced_works`**:
    *   **Description:** Retrieves the list of OpenAlex IDs cited *by* a specific OpenAlex work (outgoing citations). Note: This tool currently returns only the list of IDs. Use `get_work_details` for more info on each reference.
    *   **Parameters:**
        *   `work_id` (string, required): OpenAlex ID of the *citing* work.
    *   **Returns:** Object with `referenced_work_ids` (list of strings), or an error object.

4.  **`get_citing_works`**:
    *   **Description:** Retrieves the list of works that *cite* a specific OpenAlex work (incoming citations). Uses cursor pagination.
    *   **Parameters:**
        *   `work_id` (string, required): OpenAlex ID of the *cited* work.
        *   `select_fields` (array of strings, optional): List of root-level fields for each citing work.
        *   `per_page` (integer, optional, default: 25): Results per page (max 200).
        *   `cursor` (string, optional): Pagination cursor.
    *   **Returns:** Object with `results` (list of citing work objects) and `meta` (pagination info), or an error object.

5.  **`get_work_ngrams`**:
    *   **Description:** Retrieves the N-grams (word proximity information) for a specific OpenAlex work's full text, if available.
    *   **Parameters:**
        *   `work_id` (string, required): OpenAlex ID of the work.
    *   **Returns:** Object representing the N-grams, or an error object (e.g., if N-grams are not found).

## Setup and Installation

### Prerequisites

*   Python 3.9+

### Installation

1.  **Clone the repository (optional):**
    ```bash
    git clone <repository-url>
    cd openalex-mcp-server
    ```
2.  **Install dependencies:**
    It's recommended to use a virtual environment.
    ```bash
    # Using pip
    python -m venv .venv
    source .venv/bin/activate # On Windows use `.venv\Scripts\activate`
    pip install .

    # Or using uv
    uv venv
    source .venv/bin/activate
    uv pip install .
    ```
    This installs the server package along with its dependencies (`mcp[cli]`, `pyalex`).

### Configuration

*   **OpenAlex Polite Pool:** To use the faster, more reliable OpenAlex polite pool, set the `OPENALEX_EMAIL` environment variable to your email address *before* running the server or when configuring it in your MCP client.
    ```bash
    export OPENALEX_EMAIL="your.email@example.com"
    ```
    If this variable is not set, the server will use the anonymous pool, which has stricter rate limits.

## MCP Integration

To use this server with an MCP client (like the Claude VS Code Extension or Claude Desktop), you need to add its configuration to the client's settings file.

**Example Configuration (`cline_mcp_settings.json` or similar):**

```json
{
  "mcpServers": {
    "... other servers ...": {},
    "openalex": {
      "autoApprove": [],
      "disabled": false,
      "timeout": 60,
      "command": "openalex-mcp-server", // Uses the installed script
      "args": [],
      "env": {
        "OPENALEX_EMAIL": "your.email@example.com" // Set your email here!
      },
      "transportType": "stdio"
    }
  }
}
```

*   Replace `"your.email@example.com"` with your actual email address.
*   Ensure the `command` (`openalex-mcp-server`) is accessible in your system's PATH, which should be the case if installed correctly via pip/uv in an active environment.
*   Restart your MCP client (e.g., reload the VS Code window) after adding the configuration.

## Usage

Once the server is configured and running (either via the MCP client integration or manually for development), you can interact with it using the `use_mcp_tool` command within your AI agent.

**Example Tool Calls:**

*   **Search for papers about "machine learning" published in 2023:**
    ```xml
    <use_mcp_tool>
      <server_name>openalex</server_name>
      <tool_name>search_works</tool_name>
      <arguments>
      {
        "search_query": "machine learning",
        "filters": { "publication_year": 2023 },
        "select_fields": ["id", "doi", "title", "publication_year", "cited_by_count"]
      }
      </arguments>
    </use_mcp_tool>
    ```

*   **Get details for a specific work:**
    ```xml
    <use_mcp_tool>
      <server_name>openalex</server_name>
      <tool_name>get_work_details</tool_name>
      <arguments>
      {
        "work_id": "W2741809807",
        "select_fields": ["title", "authorships", "abstract", "open_access"]
      }
      </arguments>
    </use_mcp_tool>
    ```

*   **Get references for a work:**
    ```xml
    <use_mcp_tool>
      <server_name>openalex</server_name>
      <tool_name>get_referenced_works</tool_name>
      <arguments>
      {
        "work_id": "W2741809807"
      }
      </arguments>
    </use_mcp_tool>
    ```

## Development

To run the server locally for development and testing:

1.  Ensure dependencies are installed (see Installation).
2.  Set the `OPENALEX_EMAIL` environment variable if desired.
3.  Run the server script directly:
    ```bash
    source .venv/bin/activate # Activate your environment
    export OPENALEX_EMAIL="your.email@example.com" # Optional
    python src/openalex_mcp_server/server.py
    ```
    The server will start and listen on standard input/output for MCP messages.

    *(Note: Running with `mcp dev src/openalex_mcp_server/server.py` encountered issues during development, potentially related to script execution or initialization conflicts. Running directly with `python` was more reliable.)*

## Dependencies

*   [mcp-sdk](https://github.com/modelcontextprotocol/python-sdk): For MCP server implementation.
*   [pyalex](https://github.com/J535D165/pyalex): For interacting with the OpenAlex API.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details (if one exists).
