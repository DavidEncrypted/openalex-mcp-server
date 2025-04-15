import os
import json
import logging
from typing import List, Dict, Any, Optional, Tuple, Union
from itertools import chain

import pyalex
from pyalex import Works, Authors, Sources, Institutions, Topics, Publishers, Funders
from pyalex.api import Work
from mcp.server.fastmcp import FastMCP, Context # Corrected import
import mcp.types as mcp_types

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configure pyalex ---
# Use environment variable for email (recommended)
OPENALEX_EMAIL = os.getenv("OPENALEX_EMAIL")
if OPENALEX_EMAIL:
    pyalex.config.email = OPENALEX_EMAIL
    # logger.info(f"Using OpenAlex polite pool with email: {OPENALEX_EMAIL}") # Temporarily commented out
# else:
    # logger.warning(
    #     "OPENALEX_EMAIL environment variable not set. "
    #     "Using OpenAlex anonymous pool (slower, lower rate limits)."
    # ) # Temporarily commented out

# Configure retries for robustness
pyalex.config.max_retries = 3
pyalex.config.retry_backoff_factor = 0.5
pyalex.config.retry_http_codes = [429, 500, 503]

# --- MCP Server Setup ---
mcp = FastMCP(
    "OpenAlex Works Explorer",
    version="0.1.0",
    description="Provides tools to search and retrieve data about scholarly works from OpenAlex.",
)

# --- Helper Functions ---

def _select_fields(item: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    """Selects specific root-level fields from a dictionary."""
    # Ensure essential ID fields are always present if specific fields are requested
    required_ids = {"id", "doi"} # Add others if needed
    selected_fields_set = set(fields) | required_ids

    return {k: v for k, v in item.items() if k in selected_fields_set}

def _process_results(
    results: List[Dict[str, Any]],
    select_fields: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Processes a list of results for field selection and abstract inversion."""
    processed = []
    for item in results:
        # Convert pyalex object to dict if necessary
        if not isinstance(item, dict):
             if hasattr(item, 'to_dict'): # Check if it's a pyalex object
                 item = item.to_dict()
             else:
                 logger.warning(f"Skipping non-dictionary item: {type(item)}")
                 continue

        # Check if item is a pyalex object (duck typing: check for .items())
        is_pyalex_obj = hasattr(item, 'items') and callable(item.items)

        # If fetching full object (select_fields is None) and it's a pyalex object,
        # try to trigger abstract generation *before* converting to dict.
        generated_abstract = None
        if select_fields is None and is_pyalex_obj:
            try:
                generated_abstract = item["abstract"] # Access abstract on original object
            except KeyError:
                logger.warning(f"Abstract could not be generated for {item.get('id')}, index likely missing.")
            except Exception as gen_err:
                logger.error(f"Error during abstract generation for {item.get('id')}: {gen_err}")

        # Convert to dict *after* potential abstract access
        if is_pyalex_obj:
            dict_item = dict(item.items())
        else:
            dict_item = item # Assume it's already a dict

        # Ensure abstract is correctly placed in the dictionary if generated
        if generated_abstract is not None:
             dict_item["abstract"] = generated_abstract
        elif select_fields is None and "abstract" not in dict_item:
             # Ensure key exists as None if full object requested but abstract failed/missing
             dict_item["abstract"] = None

        # Apply field selection if requested
        if select_fields:
            processed.append(_select_fields(dict_item, select_fields))
        else:
            # Append the full (potentially abstract-added) dictionary
            processed.append(dict_item)

    return processed

def _summarize_work(work_dict: Dict[str, Any], select_fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Condenses a full work dictionary into a summary, potentially filtered by select_fields.
    """
    # Define the potential fields for the summary
    summary_data = {}

    # --- Extract potential summary fields ---
    summary_data["id"] = work_dict.get("id")
    summary_data["doi"] = work_dict.get("doi")
    summary_data["title"] = work_dict.get("title")
    summary_data["publication_year"] = work_dict.get("publication_year")
    # Fix duplication and limit to max 6 authors
    summary_data["authors"] = [
        authorship.get("author", {}).get("display_name")
        for authorship in work_dict.get("authorships", []) # Corrected: single loop
        if authorship.get("author", {}).get("display_name")
    ][:6] # Limit to 6
    summary_data["cited_by_count"] = work_dict.get("cited_by_count")

    venue_source = work_dict.get("primary_location", {}).get("source", {})
    summary_data["venue"] = venue_source.get("display_name") if venue_source else None

    # Determine best OA URL
    summary_data["oa_url"] = None
    best_oa = work_dict.get("best_oa_location")
    if best_oa and best_oa.get("pdf_url"):
        summary_data["oa_url"] = best_oa.get("pdf_url")
    elif work_dict.get("open_access", {}).get("oa_url"):
         summary_data["oa_url"] = work_dict.get("open_access", {}).get("oa_url")

    # Abstract should be generated by pyalex when accessed if full object fetched
    summary_data["abstract"] = work_dict.get("abstract") # Accessing this might trigger generation
    # --- End Extraction ---

    # Determine which fields to include in the final summary
    default_summary_keys = set(summary_data.keys())
    if select_fields is None:
        keys_to_include = default_summary_keys
    else:
        keys_to_include = default_summary_keys.intersection(set(select_fields))

    # Construct final summary dictionary
    final_summary = {key: summary_data[key] for key in keys_to_include if summary_data[key] is not None}

    return final_summary

# --- MCP Tools ---

@mcp.tool()
async def search_works(
    ctx: Context, # Changed ToolContext to Context
    search_query: str,
    filters: Optional[Dict[str, Any]] = None,
    search_field: str = "default",
    select_fields: Optional[List[str]] = None,
    sort: Optional[Dict[str, str]] = None,
    summarize_results: bool = True, # Added parameter
    per_page: int = 25,
    cursor: Optional[str] = "*",
) -> Dict[str, Any]:
    """
    Searches for OpenAlex works based on keywords and filters, returning selected fields.
    Supports boolean operators in the search query as per OpenAlex syntax. Uses cursor pagination.

    Args:
        search_query: Search term(s). Supports OpenAlex boolean/proximity operators, DO NOT use any quotation marks in the query.
        filters: Key-value pairs for filtering. See OpenAlex docs for keys and value formats.
                 Use '|' for OR, '!' for NOT.
        search_field: Field to search within ('title', 'abstract', 'fulltext',
                      'title_and_abstract', 'default'). Default searches title,
                      abstract, and fulltext.
        select_fields: List of root-level fields to return. See OpenAlex docs for options.
                       If summarize_results is True, this filters the *summarized* fields.
                       If summarize_results is False, this directly selects fields from OpenAlex (or returns full object if None).
        sort: Field to sort by and direction (e.g., {"cited_by_count": "desc"}).
              Common fields: cited_by_count, publication_date, relevance_score.
        summarize_results: If True (default), returns a condensed summary of each work
                           (id, doi, title, year, authors, citation count, venue, oa_url, abstract),
                           potentially filtered by select_fields. If False, returns more detailed
                           results based on select_fields (or the full object if select_fields is None).
        per_page: Number of results per page (1-200). Default: 25.
        cursor: Cursor for pagination (preferred method). Use '*' for the first page.

    Returns:
        A dictionary containing the search results and pagination metadata.
    """
    logging.info("running search_works")
    try:
        query = Works()

        # Apply search query and field
        if search_field == "default":
            query = query.search(search_query)
        elif search_field in ["title", "abstract", "fulltext", "title_and_abstract", "display_name"]:
            # pyalex uses search_filter for specific fields
            search_filter_dict = {search_field: search_query}
            # Handle display_name alias for title
            if search_field == "display_name":
                search_filter_dict = {"title": search_query}
            logging.info("running search_works query!")
            query = query.search_filter(**search_filter_dict)
        else:
            raise ValueError(f"Invalid search_field: {search_field}")

        logging.info(f"{query.count()} number of results found pre filters")

        # Apply filters
        if filters:
            # pyalex expects filters as keyword arguments
            # Need to handle nested filters like authorships.institutions.ror
            processed_filters = {}
            for key, value in filters.items():
                # Basic handling for nested keys, pyalex might need dicts for these
                if '.' in key:
                     # pyalex handles nested filters via dicts passed as kwargs
                     # e.g. authorships={"institutions": {"ror": "value"}}
                     # We assume the user provides the filter key correctly for pyalex
                     # This might need refinement based on how pyalex expects complex filters
                     logger.warning(f"Nested filter key '{key}' passed directly. Ensure pyalex compatibility.")
                     processed_filters[key] = value # Pass as is, pyalex might handle it
                else:
                    processed_filters[key] = value
            query = query.filter(**processed_filters)

        # Apply sorting
        if sort:
            query = query.sort(**sort)

        # Determine if we need to select specific fields based on summarize_results
        if not summarize_results and select_fields:
            # Only apply select_fields if summarization is OFF and fields are provided
            logger.info(f"Summarization off, selecting fields: {select_fields}")
            query = query.select(select_fields)
        # Otherwise (summarize=True OR select_fields=None), fetch the full object

        # Execute paginated query
        # pyalex paginate returns an iterator of pages
        # if cursor:
        pager = query.paginate(per_page=per_page, cursor=cursor, n_max=per_page) # n_max=per_page gets just one page
        # else:
        #     pager = query.paginate(per_page=per_page, cursor="*", n_max=per_page) # n_max=per_page gets just one page

        page_results = []
        metadata = {}
        try:
            # Get the first (and only requested) page
            first_page = next(pager)
            page_results = first_page
            # Extract metadata which includes the next cursor
            if hasattr(first_page, 'meta'):
                 metadata = first_page.meta
            else:
                 # Fallback if meta isn't directly on the page list (might happen with empty results?)
                 # Try to get meta from the underlying query object if possible (pyalex internal detail)
                 if hasattr(query, '_get_meta'):
                     try:
                         # This is accessing internal pyalex state, might break
                         raw_meta_response = await query._get_meta()
                         metadata = raw_meta_response.get('meta', {})
                     except Exception as meta_err:
                         logger.warning(f"Could not retrieve metadata via internal fallback: {meta_err}")
                 else:
                     logger.warning("Could not retrieve pagination metadata.")

        except StopIteration:
            # No results found for this cursor/query
            logger.info("No results found for the current page.")
            metadata = {'count': 0, 'page': 1, 'per_page': per_page, 'next_cursor': None} # Synthesize meta

        # Process results (convert to dict, potentially trigger abstract generation)
        # Pass select_fields=None to _process_results because if fields were selected,
        # pyalex did it during the query. If not, we need the full dict processed.
        processed_page_results = _process_results(page_results, None)

        # REMOVED: Explicit abstract trigger loop here, logic moved into _process_results

        # Apply summarization if requested
        if summarize_results:
            # Pass select_fields to the summarizer to filter the summary
            final_results = [_summarize_work(item, select_fields) for item in processed_page_results]
        else:
            # If not summarizing, the results are already processed (either full or selected by pyalex)
            final_results = processed_page_results

        # Construct response
        response = {
            "results": final_results, # Use the potentially summarized results
            "meta": {
                "count": metadata.get("count"),
                "per_page": metadata.get("per_page", per_page),
                "next_cursor": metadata.get("next_cursor"), # Key for pagination
            },
        }
        return response

    except Exception as e:
        logger.exception(f"Error in search_works: {e}")
        # Return error information in a structured way if possible
        return {"error": str(e), "results": [], "meta": {}}
@mcp.tool()
async def get_work_details(
    ctx: Context, # Changed ToolContext to Context
    work_id: str,
    select_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Retrieves detailed information for a specific OpenAlex work by its ID
    (OpenAlex ID URL, DOI URL, PMID URL, MAG ID). Handles abstract generation, no need to request abstract_inverted_index, simple retrieve abstract.

    Args:
        work_id: Identifier for the work (OpenAlex ID URL, DOI URL, PMID URL, MAG ID).
        select_fields: List of root-level fields to return. See OpenAlex docs for options.
                       If omitted, returns the full object.

    Returns:
        A dictionary containing the work details or an error message.
    """
    try:
        final_result = {}
        # Determine if we need the full object (for abstract generation or full details)
        fetch_full_object = not select_fields or (select_fields and "abstract" in select_fields)

        if fetch_full_object:
            logger.info("Fetching full work object for abstract generation or full details.")
            work_data: Work = Works()[work_id]
            if not work_data: return {"error": f"Work not found: {work_id}"}

            # Create a base dictionary from the Work object's items
            # This captures all readily available fields
            work_dict = dict(work_data.items())

            # Separately attempt to generate and add the abstract
            try:
                # Accessing work_data['abstract'] triggers pyalex's internal generation
                generated_abstract = work_data["abstract"]
                work_dict["abstract"] = generated_abstract # Add/overwrite abstract in our dict
            except KeyError:
                 # This might happen if abstract_inverted_index is missing/null
                 logger.warning(f"Abstract could not be generated for {work_id}, index likely missing.")
                 work_dict["abstract"] = None # Ensure abstract field exists as None
            except Exception as gen_err:
                 logger.error(f"Error during abstract generation for {work_id}: {gen_err}")
                 work_dict["abstract"] = None # Ensure abstract field exists as None

            # Apply user's selection if provided
            if select_fields:
                final_result = _select_fields(work_dict, select_fields)
            else:
                final_result = work_dict # Use the full dictionary

            # Remove index if abstract exists and index wasn't explicitly requested
            should_remove_index = (
                "abstract" in final_result and final_result.get("abstract") is not None and
                "abstract_inverted_index" in final_result and
                (not select_fields or "abstract_inverted_index" not in select_fields)
            )
            if should_remove_index:
                del final_result["abstract_inverted_index"]

        else:
            # Abstract not requested, use pyalex select() for efficiency
            logger.info("Abstract not requested, using pyalex select() for efficiency.")
            # Ensure essential IDs are always included
            query_select = list(set(select_fields) | {"id", "doi"})
            work_data: Work = Works().select(query_select)[work_id]
            if not work_data: return {"error": f"Work not found: {work_id}"}

            # Convert selected data to dict
            final_result = dict(work_data.items())

        return final_result

    except Exception as e:
        logger.exception(f"Error in get_work_details for ID {work_id}: {e}")
        return {"error": str(e)}

# --- New Batch Tool ---
@mcp.tool()
async def get_batch_work_details(
    ctx: Context,
    work_ids: List[str],
    select_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Retrieves detailed information for a list of OpenAlex works by their IDs.
    Limited to a maximum of 50 IDs per request due to API limitations.

    Args:
        work_ids: A list of OpenAlex work identifiers (max 50).
        select_fields: List of root-level fields to return for each work.
                       See OpenAlex docs for options. If omitted, returns full objects.

    Returns:
        A dictionary containing a list of work details under the 'works' key,
        or an error message.
    """
    MAX_IDS = 50
    if not work_ids:
        return {"error": "work_ids list cannot be empty.", "works": []}
    if len(work_ids) > MAX_IDS:
        return {"error": f"Too many work_ids provided. Maximum is {MAX_IDS}.", "works": []}

    try:
        # Clean IDs (remove potential URL prefixes)
        cleaned_ids = [
            _id.split("/")[-1] if _id.startswith("https://openalex.org/") else _id
            for _id in work_ids
        ]

        # Construct the filter string for the API
        id_filter = {"ids": {"openalex": "|".join(cleaned_ids)}}

        # Build the query
        query = Works().filter(**id_filter)

        # Check if abstract is requested and handle it specially
        abstract_requested = False
        api_select_fields = None
        
        if select_fields:
            # Check if abstract is in the requested fields
            abstract_requested = "abstract" in select_fields
            
            # Create a new list without 'abstract' for the API query
            api_select_fields = [field for field in select_fields if field != "abstract"]
            
            # If abstract was requested, ensure we get the inverted index
            if abstract_requested and "abstract_inverted_index" not in api_select_fields:
                api_select_fields.append("abstract_inverted_index")
            
            # Ensure 'id' is always selected
            if "id" not in api_select_fields:
                api_select_fields = ["id"] + api_select_fields
                
            # Apply field selection to the query
            query = query.select(api_select_fields)
        
        # Fetch all results for the given IDs.
        results_list = query.get() # Use get() which returns a list

        # For each work, process the abstract if it was requested
        processed_results = []
        for work in results_list:
            # Convert to dict if needed
            if not isinstance(work, dict):
                if hasattr(work, 'to_dict'):
                    work_dict = work.to_dict()
                elif hasattr(work, 'items') and callable(work.items):
                    work_dict = dict(work.items())
                else:
                    logger.warning(f"Skipping non-dictionary item: {type(work)}")
                    continue
            else:
                work_dict = work

            # Generate abstract if it was requested or if no fields were specified
            if abstract_requested or not select_fields:
                if "abstract_inverted_index" in work_dict:
                    try:
                        # Try to access the abstract to trigger pyalex's generation
                        if hasattr(work, "__getitem__"):
                            generated_abstract = work["abstract"]
                        else:
                            # Try to generate from the inverted index
                            # This is a fallback if direct access doesn't work
                            inverted_index = work_dict.get("abstract_inverted_index")
                            if inverted_index:
                                # Simple reconstruction of abstract from inverted index
                                # This is a basic implementation - pyalex might do something more sophisticated
                                words = []
                                for word, positions in inverted_index.items():
                                    for pos in positions:
                                        while len(words) <= pos:
                                            words.append("")
                                        words[pos] = word
                                generated_abstract = " ".join(words)
                            else:
                                generated_abstract = None
                        
                        # Store the generated abstract
                        work_dict["abstract"] = generated_abstract
                    except KeyError:
                        logger.warning(f"Abstract could not be generated for {work_dict.get('id')}, index likely missing.")
                        work_dict["abstract"] = None
                    except Exception as gen_err:
                        logger.error(f"Error during abstract generation for {work_dict.get('id')}: {gen_err}")
                        work_dict["abstract"] = None
                else:
                    # No inverted index available
                    logger.warning(f"No abstract_inverted_index available for {work_dict.get('id')}")
                    work_dict["abstract"] = None

            # Create the result dictionary based on the requested fields
            if select_fields:
                # If abstract was requested but filtered out for the API query,
                # we need to add it back to the select_fields for _select_fields
                if abstract_requested:
                    select_fields_with_abstract = list(api_select_fields) + ["abstract"]
                    result = _select_fields(work_dict, select_fields_with_abstract)
                else:
                    result = _select_fields(work_dict, api_select_fields)
            else:
                result = work_dict

            # Handle abstract_inverted_index based on what was requested
            inverted_index_requested = select_fields and "abstract_inverted_index" in select_fields
            
            # Remove inverted index if it wasn't explicitly requested
            if "abstract_inverted_index" in result and not inverted_index_requested:
                del result["abstract_inverted_index"]

            processed_results.append(result)

        # Return the list of work details
        return {"works": processed_results}

    except Exception as e:
        logger.exception(f"Error in get_batch_work_details for IDs {work_ids}: {e}")
        # Consider more specific error handling if pyalex raises identifiable errors
        return {"error": str(e), "works": []}
# --- End New Batch Tool ---

@mcp.tool()
async def get_referenced_works(
    ctx: Context, # Changed ToolContext to Context
    work_id: str,
) -> Dict[str, Any]:
    """
    Retrieves the list of OpenAlex IDs cited *by* a specific OpenAlex work (outgoing citations).
    Returns only the list of IDs. Use get_work_details for more info on each reference.

    Args:
        work_id: OpenAlex ID of the *citing* work (the one whose references you want).

    Returns:
        A dictionary containing a list of referenced work IDs under the
        'referenced_work_ids' key, or an error message.
    """
    try:
        # Ensure work_id is just the ID part if a full URL is passed
        if work_id.startswith("https://openalex.org/"):
            work_id = work_id.split("/")[-1]

        work_data = Works()[work_id]
        referenced_ids = work_data.get("referenced_works", [])
        return {"referenced_work_ids": referenced_ids}

    except Exception as e:
        logger.exception(f"Error in get_referenced_works for ID {work_id}: {e}")
        return {"error": str(e), "referenced_work_ids": []}
@mcp.tool()
async def get_citing_works(
    ctx: Context, # Changed ToolContext to Context
    work_id: str,
    select_fields: Optional[List[str]] = None,
    summarize_results: bool = True, # Added parameter
    per_page: int = 25,
    cursor: Optional[str] = "*",
) -> Dict[str, Any]:
    """
    Retrieves the list of works that *cite* a specific OpenAlex work (incoming citations).
    Uses cursor pagination.

    Args:
        work_id: OpenAlex ID of the *cited* work (the one you want citations for).
        select_fields: List of root-level fields to return. See OpenAlex docs for options.
                       If summarize_results is True, this filters the *summarized* fields.
                       If summarize_results is False, this directly selects fields from OpenAlex
                       (or returns default summary fields if None).
        summarize_results: If True (default), returns a condensed summary of each citing work.
                           If False, returns more detailed results based on select_fields.
        per_page: Number of results per page (1-200). Default: 25.
        cursor: Cursor for pagination.

    Returns:
        A dictionary containing the citing works results and pagination metadata,
        or an error message.
    """
    try:
        # Ensure work_id is just the ID part if a full URL is passed
        if work_id.startswith("https://openalex.org/"):
            work_id = work_id.split("/")[-1]

        query = Works().filter(cites=work_id)

        # Determine if we need to select specific fields based on summarize_results
        if not summarize_results and select_fields:
            # Only apply select_fields if summarization is OFF and fields are provided
            logger.info(f"Summarization off, selecting fields for citing works: {select_fields}")
            query = query.select(select_fields)
        elif not summarize_results and not select_fields:
            # Summarization off, no fields specified - use default summary fields for efficiency
            default_summary_fields = ["id", "doi", "title", "publication_year", "authorships", "cited_by_count", "primary_location", "open_access", "abstract"]
            logger.info(f"Summarization off, no fields specified, selecting default summary fields: {default_summary_fields}")
            query = query.select(default_summary_fields)
        # Otherwise (summarize=True), fetch the full object to allow summarization

        # Execute paginated query
        pager = query.paginate(per_page=per_page, cursor=cursor, n_max=per_page)

        page_results = []
        metadata = {}
        try:
            first_page = next(pager)
            page_results = first_page
            if hasattr(first_page, 'meta'):
                 metadata = first_page.meta
            else:
                 # Fallback attempt (see search_works for explanation)
                 if hasattr(query, '_get_meta'):
                     try:
                         raw_meta_response = await query._get_meta()
                         metadata = raw_meta_response.get('meta', {})
                     except Exception as meta_err:
                         logger.warning(f"Could not retrieve metadata via internal fallback: {meta_err}")
                 else:
                     logger.warning("Could not retrieve pagination metadata.")

        except StopIteration:
            logger.info(f"No citing works found for page with cursor {cursor}.")
            metadata = {'count': 0, 'page': 1, 'per_page': per_page, 'next_cursor': None} # Synthesize meta

        # Process results (convert to dict, potentially trigger abstract generation)
        # Pass select_fields=None if summarizing, as we fetched the full object
        # Pass original select_fields if not summarizing, as pyalex handled selection
        processed_page_results = _process_results(page_results, None if summarize_results else select_fields)

        # Apply summarization if requested
        if summarize_results:
            # Pass original select_fields to the summarizer to filter the summary
            final_results = [_summarize_work(item, select_fields) for item in processed_page_results]
        else:
            # If not summarizing, the results are already processed (either full or selected by pyalex)
            final_results = processed_page_results

        response = {
            "results": final_results, # Use the potentially summarized results
            "meta": {
                "count": metadata.get("count"),
                "per_page": metadata.get("per_page", per_page),
                "next_cursor": metadata.get("next_cursor"),
            },
        }
        return response

    except Exception as e:
        logger.exception(f"Error in get_citing_works for ID {work_id}: {e}")
        return {"error": str(e), "results": [], "meta": {}}
@mcp.tool()
async def get_work_ngrams(
    ctx: Context, # Changed ToolContext to Context
    work_id: str,
) -> Dict[str, Any]:
    """
    Retrieves the N-grams (word proximity information) for a specific OpenAlex work's full text.

    Args:
        work_id: OpenAlex ID of the work.

    Returns:
        A dictionary containing the N-gram data or an error message.
    """
    try:
        # Ensure work_id is just the ID part if a full URL is passed
        if work_id.startswith("https://openalex.org/"):
            work_id = work_id.split("/")[-1]

        # pyalex provides ngrams() method on a Work object
        ngrams_data = Works()[work_id].ngrams()
        return ngrams_data # Should already be a dictionary

    except Exception as e:
        logger.exception(f"Error in get_work_ngrams for ID {work_id}: {e}")
        # Check if it's a known pyalex/API error (e.g., 404 Not Found if ngrams don't exist)
        # pyalex might raise requests.exceptions.HTTPError
        if hasattr(e, 'response') and e.response.status_code == 404:
             return {"error": f"N-grams not found for work ID {work_id}."}
        return {"error": str(e)}

# --- Main Execution ---
def main():
    """Runs the MCP server."""
    logging.warning("Starting OpenAlex MCP Server!")
    mcp.run()

if __name__ == "__main__":
    main()
