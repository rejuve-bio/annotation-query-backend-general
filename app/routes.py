from flask import (
    copy_current_request_context,
    request,
    jsonify,
    Response,
    send_from_directory,
)
import logging
import json
import os
import threading
from app import app, schema_manager, socketio, redis_client
from app.lib import validate_request
from flask_cors import CORS
from flask_socketio import disconnect, join_room, send

# from app.lib import limit_graph
from dotenv import load_dotenv
from distutils.util import strtobool
import datetime
from app.lib import Graph, heuristic_sort
from app.annotation_controller import handle_client_request, requery
from app.constants import TaskStatus
from app.workers.task_handler import get_annotation_redis
from app.persistence import AnnotationStorageService
from app.services.cypher_generator import CypherQueryGenerator
from app.services.metta_generator import MeTTa_Query_Generator
from app import config
import requests

# Load environmental variables
load_dotenv()

# set mongo logging
logging.getLogger("pymongo").setLevel(logging.CRITICAL)

# set redis logging
logging.getLogger("flask_redis").setLevel(logging.CRITICAL)

llm = app.config["llm_handler"]
EXP = os.getenv("REDIS_EXPIRATION", 3600)  # expiration time of redis cache
CORS(app)



@app.route("/schema", methods=["GET"])
def get_schema():
    response = schema_by_source()
    if isinstance(response, list):
        response = {}
        response["schema"] = schema_manager.schema
    
    if len(response['schema']['nodes']) == 0:
        response = {}
        response["schema"] = schema_manager.schema

    return Response(json.dumps(response, indent=4), mimetype='application/json')

@socketio.on("connect")
def on_connect(args):
    logging.info("source connected")
    send("source is connected")


@socketio.on("disconnect")
def on_disconnect():
    logging.info("source disconnected")
    send("source Disconnected")
    disconnect()


@socketio.on("join")
def on_join(data):
    room = data["room"]
    join_room(room)
    logging.info(f"source join a room with {room}")
    # send(f'connected to {room}', to=room)
    cache = get_annotation_redis(room)

    if cache != None:
        status = cache["status"]
        graph = cache["graph"]
        graph_status = True if graph is not None else False

        if status == TaskStatus.COMPLETE.value:
            socketio.emit(
                "update",
                {"status": status, "update": {"graph": graph_status}},
                to=str(room),
            )


@app.route("/query", methods=["POST"])  # type: ignore
def process_query():
    data = request.get_json()
    if not data or "requests" not in data:
        return jsonify({"error": "Missing requests data"}), 400

    limit = request.args.get("limit")
    properties = request.args.get("properties")

    if properties:
        properties = bool(strtobool(properties))
    else:
        properties = True

    if limit:
        try:
            limit = int(limit)
        except ValueError:
            return (
                jsonify({"error": "Invalid limit value. It should be an integer."}),
                400,
            )
    else:
        limit = None
    try:
        requests = data["requests"]

        # Validate the request data before processing
        node_map = validate_request(requests, schema_manager.schema)
        if node_map is None:
            return (
                jsonify({"error": "Invalid node_map returned by validate_request"}),
                400,
            )

        # sort the predicate based on the the edge count
        if os.getenv("HURISTIC_SORT", "False").lower() == "true":
            requests = heuristic_sort(requests, node_map)

        db_instance = app.config["db_instance"]
        # Generate the query code
        query = db_instance.query_Generator(requests, node_map, limit)

        # Extract node types
        nodes = requests["nodes"]
        node_types = set()

        for node in nodes:
            node_types.add(node["type"])

        node_types = list(node_types)

        return handle_client_request(query, requests, node_types)
    except Exception as e:
        logging.error(f"Error processing query: {e}")
        return jsonify({"error": (e)}), 500


@app.route("/history", methods=["GET"])
def process_source_history():
    job_id = app.config.get("job_id", None)
    return_value = []

    if not job_id:
        return jsonify("No Job id found load or select data first"), 400

    cursor = AnnotationStorageService.get(job_id)

    if cursor is None:
        return jsonify("No value Found"), 200

    for document in cursor:
        return_value.append(
            {
                "annotation_id": str(document["_id"]),
                "request": document["request"],
                "title": document["title"],
                "node_count": document["node_count"],
                "edge_count": document["edge_count"],
                "node_types": document["node_types"],
                "status": document["status"],
                "created_at": document["created_at"].isoformat(),
                "updated_at": document["updated_at"].isoformat(),
            }
        )
    return Response(json.dumps(return_value, indent=4), mimetype="application/json")


@app.route("/annotation/<id>", methods=["GET"])
def get_by_id(id):
    response_data = {}
    cursor = AnnotationStorageService.get_by_id(id)

    if cursor is None:
        return jsonify("No value Found"), 404

    limit = request.args.get("limit")
    properties = request.args.get("properties")

    if properties:
        properties = bool(strtobool(properties))
    else:
        properties = False

    if limit:
        try:
            limit = int(limit)
        except ValueError:
            return (
                jsonify({"error": "Invalid limit value. It should be an integer."}),
                400,
            )

    json_request = cursor.request
    query = cursor.query
    title = cursor.title
    summary = cursor.summary
    annotation_id = cursor.id
    node_count = cursor.node_count
    edge_count = cursor.edge_count
    node_count_by_label = cursor.node_count_by_label
    edge_count_by_label = cursor.edge_count_by_label
    status = cursor.status
    file_path = cursor.path_url

    # Extract node types
    nodes = json_request["nodes"]
    node_types = set()
    for node in nodes:
        node_types.add(node["type"])
    node_types = list(node_types)

    try:
        response_data["annotation_id"] = str(annotation_id)
        response_data["request"] = json_request
        response_data["title"] = title

        if summary is not None:
            response_data["summary"] = summary
        if node_count is not None:
            response_data["node_count"] = node_count
            response_data["edge_count"] = edge_count
        if node_count_by_label is not None:
            response_data["node_count_by_label"] = node_count_by_label
            response_data["edge_count_by_label"] = edge_count_by_label
        response_data["status"] = status

        cache = redis_client.get(str(annotation_id))

        if cache is not None:
            cache = json.loads(cache)
            graph = cache["graph"]
            if graph is not None:
                response_data["nodes"] = graph["nodes"]
                response_data["edges"] = graph["edges"]

            return Response(
                json.dumps(response_data, indent=4), mimetype="application/json"
            )

        if status in [TaskStatus.PENDING.value, TaskStatus.COMPLETE.value]:
            if status == TaskStatus.COMPLETE.value:
                if os.path.exists(file_path):
                    # open the file and read the graph
                    with open(file_path, "r") as file:
                        graph = json.load(file)

                    response_data["nodes"] = graph["nodes"]
                    response_data["edges"] = graph["edges"]
                else:
                    response_data["status"] = TaskStatus.PENDING.value
                    requery(annotation_id, query, json_request)
            formatted_response = json.dumps(response_data, indent=4)
            return Response(formatted_response, mimetype="application/json")

        db_instance = app.config["db_instance"]
        # Run the query and parse the results
        result = db_instance.run_query(query)
        graph_components = {"properties": properties}
        response_data = db_instance.parse_and_serialize(
            result, schema_manager.schema, graph_components, result_type="graph"
        )
        graph = Graph()
        if len(response_data["edges"]) == 0:
            response_data = graph.group_node_only(response_data, request)
        else:
            grouped_graph = graph.group_graph(response_data)
        response_data["nodes"] = grouped_graph["nodes"]
        response_data["edges"] = grouped_graph["edges"]

        formatted_response = json.dumps(response_data, indent=4)
        return Response(formatted_response, mimetype="application/json")
    except Exception as e:
        logging.error(f"Error processing query: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/annotation/<id>", methods=["DELETE"])
def delete_by_id(id):
    try:
        # check if the source have access to delete the resource
        annotation = AnnotationStorageService.get_by_id(id)

        if annotation is None:
            return jsonify("No value Found"), 404

        # first check if there is any running running annoation
        with app.config["annotation_lock"]:
            thread_event = app.config["annotation_threads"]
            stop_event = thread_event.get(id, None)

            # if there is stop the running annoation
            if stop_event is not None:
                stop_event.set()

                response_data = {"message": f"Annotation {id} has been cancelled."}

                formatted_response = json.dumps(response_data, indent=4)
                return Response(formatted_response, mimetype="application/json")

        # else delete the annotation from the db
        existing_record = AnnotationStorageService.get_by_id(id)

        if existing_record is None:
            return jsonify("No value Found"), 404

        deleted_record = AnnotationStorageService.delete(id)

        if deleted_record is None:
            return jsonify("Failed to delete the annotation"), 500

        response_data = {"message": "Annotation deleted successfully"}

        formatted_response = json.dumps(response_data, indent=4)
        return Response(formatted_response, mimetype="application/json")
    except Exception as e:
        logging.error(f"Error deleting annotation: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/annotation/<id>/title", methods=["PUT"])
def update_title(id):
    data = request.get_json()

    if "title" not in data:
        return jsonify({"error": "Title is required"}), 400

    title = data["title"]

    try:
        existing_record = AnnotationStorageService.get_by_id(id)

        if existing_record is None:
            return jsonify("No value Found"), 404

        AnnotationStorageService.update(id, {"title": title})

        response_data = {
            "message": "title updated successfully",
            "title": title,
        }

        formatted_response = json.dumps(response_data, indent=4)
        return Response(formatted_response, mimetype="application/json")
    except Exception as e:
        logging.error(f"Error updating title: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/annotation/delete", methods=["POST"])
def delete_many():
    data = request.data.decode(
        "utf-8"
    ).strip()  # Decode and strip the string of any extra spaces or quotes

    # Ensure that data is not empty or just quotes
    if not data or data.startswith("'") and data.endswith("'"):
        data = data[1:-1]  # Remove surrounding quotes

    try:
        data = json.loads(data)  # Now parse the cleaned string
    except json.JSONDecodeError:
        return {"error": "Invalid JSON"}, 400  # Return 400 if the JSON is invalid

    if "annotation_ids" not in data:
        return jsonify({"error": "Missing annotation ids"}), 400

    annotation_ids = data["annotation_ids"]

    # check if source have access to delete the resource
    for annotation_id in annotation_ids:
        annotation = AnnotationStorageService.get_by_id(annotation_id)
        if annotation is None:
            return jsonify("No value Found"), 404

    if not isinstance(annotation_ids, list):
        return jsonify({"error": "Annotation ids must be a list"}), 400

    if len(annotation_ids) == 0:
        return jsonify({"error": "Annotation ids must not be empty"}), 400

    try:
        delete_count = AnnotationStorageService.delete_many_by_id(annotation_ids)

        response_data = {
            "message": f"Out of {len(annotation_ids)}, {delete_count} were successfully deleted."
        }

        formatted_response = json.dumps(response_data, indent=4)
        return Response(formatted_response, mimetype="application/json")
    except Exception as e:
        logging.error(f"Error deleting annotations: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/annotation/load", methods=["POST"])
def load_data():
    try:
        data = request.get_json()

        if "folder_id" not in data:
            return jsonify({"error": "folder_id is required"}), 400

        if "type" not in data:
            return jsonify({"error": "type is required"}), 400

        type = data["type"]
        folder_id = data["folder_id"]

        schema_path = f"/shared/output/{folder_id}/schema.json"
        data_path = f"/shared/output/{folder_id}/"

        app.config["job_id"] = folder_id

        # Load schema
        schema_manager.load_schema(schema_path)

        # load database config
        databases = {
            "metta": lambda: MeTTa_Query_Generator(data_path),
            "cypher": lambda: CypherQueryGenerator(data_path),
            # Add other database instances here
        }

        db_instance = databases[type]()

        if type == 'cypher':
            db_instance.set_tenant_id(folder_id)

        app.config["db_instance"] = db_instance

        response = {
            "message": "Schema loaded and data loaded successfully",
        }

        formatted_response = json.dumps(response, indent=4)

        return Response(formatted_response, mimetype="application/json")
    except Exception as e:
        logging.error(f"Error loading schema: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/run-query", methods=["POST"])
def run_query_directly():
    try:
        data = request.get_json()

        query = data['query']

        db_instance = app.config["db_instance"]

        result = db_instance.run_query(query)

        parsed_query, result = db_instance.prepare_query_input(result, schema_manager.schema)

        nodes, edges = db_instance.parse_and_seralize_no_properties(parsed_query)

        response = {
            "nodes": nodes,
            "edges": edges
        }

        formatted_response = json.dumps(response, indent=4)

        return Response(formatted_response, mimetype="application/json")
    except Exception as e:
        logging.error(f"Error running query: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/load-schema-external", methods=["GET"])
def get_neo4j_schema():
    try:
        data_path = "/"
        # load database config
        databases = {
            "metta": lambda: MeTTa_Query_Generator(data_path),
            "cypher": lambda: CypherQueryGenerator(data_path),
            # Add other database instances here
        }

        db_instance = databases["cypher"]()

        app.config["db_instance"] = db_instance

        result = db_instance.get_schema()

        #Load Schema
        schema_manager.schema = result

        formatted_response = json.dumps(schema_manager.schema, indent=4)

        return Response(formatted_response, mimetype="application/json")
    except Exception as e:
        logging.error(f"Error getting schema: {e}")
        return jsonify({"error": str(e)}), 500


def schema_by_source():
    try:
        response = {'schema': {'nodes': [], 'edges': []}}

        schema = schema_manager.schema

        for key, value in schema['nodes'].items():
            response['schema']['nodes'].append({
                'data': {
                'name': value['label'],
                'properites':[property for property in value['properties'].keys()]
                }
            })

        for key, value in schema['edges'].items():
            is_new = True
            for ed in response['schema']['edges']:
                if value['source'] == ed['data']['source'] and value['target'] == ed['data']['target']:
                    is_new = False
                    ed['data']['possible_connection'].append( value.get('input_label') or value.get('output_label') or 'unknown')
            if is_new:
                response['schema']['edges'].extend(flatten_edges(value))

        return response
    except Exception as e:
        logging.error(f"Error fetching schema: {e}")
        return []

def node_exists(response, name):
    name = name.strip().lower()
    return any(n['data']['name'].strip().lower() == name for n in response['schema']['nodes'])

def flatten_edges(value):
    sources = value['source'] if isinstance(value['source'], list) else [value['source']]
    targets = value['target'] if isinstance(value['target'], list) else [value['target']]
    label = value.get('input_label') or value.get('output_label') or 'unknown'

    return [
        {'data': {
            'source': src,
            'target': tgt,
            'possible_connection': [label]
            }
        for src in sources
        for tgt in targets
        }
    ]
