import os
from dotenv import load_dotenv
from app.services.llm_models import OpenAIModel, GeminiModel
from app.services.graph_handler import Graph_Summarizer
import logging

load_dotenv()

class LLMHandler:
    def __init__(self):
        model_type = os.getenv('LLM_MODEL')

        if model_type == 'openai':
            openai_api_key = os.getenv('OPENAI_API_KEY')
            if not openai_api_key:
                self.model = None
            else:
                self.model = OpenAIModel(openai_api_key)
        elif model_type == 'gemini':
            gemini_api_key = os.getenv('GEMINI_API_KEY')
            if not gemini_api_key:
                self.model = None
            else:
                self.model = GeminiModel(gemini_api_key)
        else:
            raise ValueError("Invalid model type in configuration")

    def generate_title(self, query, request=None, node_map=None):
        try:
            if self.model is None:
                if request is None or node_map is None:
                    return "Untitled"
                else:
                    title = self.generate_title_no_llm(request, node_map)
                    return title
            prompt = f'''From this query generate approperiate title. Only give the title sentence don't add any prefix.
                         Query: {query}'''
            title = self.model.generate(prompt)
            return title
        except Exception as e:
            logging.error("Error generating title: ", {e})
            if request is None or node_map is None:
                return "Untitled"
            else:
                title = self.generate_title_no_llm(request, node_map)
                return title
    
    def generate_title_no_llm(self, req, node_map):
        predicates = req['predicates']

        title = "Explore"

        if len(predicates) == 0:
            for id, node in node_map.items():
                title += f" {node['type'].replace('_', ' ').title()} Node, "

            return title.rstrip(", ").rstrip()

        for predicate in predicates:
            source = node_map[predicate['source']]['type']
            target = node_map[predicate['target']]['type']
            rel = predicate['type']

            title += f" {source.replace('_', ' ').title()} {rel.replace('_', ' ').title()} {target.replace('_', ' ').title()}, "

        return title.rstrip(", ").rstrip()

    def generate_summary(self, graph, request, user_query=None,graph_id=None, summary=None):
        try:
            if self.model is None:
                return "No summary available"
            summarizer = Graph_Summarizer(self.model)
            summary = summarizer.summary(graph, request, user_query, graph_id, summary)
            return summary
        except Exception as e:
            logging.error("Error generating summary: ", e)
            return "No summary available"