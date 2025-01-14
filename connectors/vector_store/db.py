from flask_sqlalchemy import SQLAlchemy
from langchain_community.docstore.document import Document
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores.pgvector import PGVector
from langchain.text_splitter import CharacterTextSplitter


db_connection_string = 'postgresql://citrus:citrus@localhost/citrus'
vector_collection_name = 'collection'

MODEL_SOURCE = "huggingface"

db = SQLAlchemy()

class Agents(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agent_name = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=False)
    system_prompt = db.Column(db.Text, nullable=True)
    filenames = db.Column(db.ARRAY(db.String), default=[], nullable=True)

    def __repr__(self):
        return f'<Agents {self.id}>'


class VectorStoreInterface():
    def __init__(self):
        self.store = None
        self.vector_chunk_size = 500
        self.vector_chunk_overlap = 0

        if MODEL_SOURCE == "ollama":
            self.embeddings = OllamaEmbeddings(model="mistral")
        elif MODEL_SOURCE == "huggingface":
            self.embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

    def init_vector_store(self):
        try:
            self.store = PGVector(
                collection_name=vector_collection_name,
                connection_string=db_connection_string,
                embedding_function=self.embeddings,
            )
        except Exception as e:
            print(f"Error init_vector_store: {e}")
        return

    def add_document(self, text, agent_id):
        documents = [Document(page_content=text, metadata={"agent_id": agent_id})]
        text_splitter = CharacterTextSplitter(chunk_size=self.vector_chunk_size, chunk_overlap=self.vector_chunk_overlap)
        docs = text_splitter.split_documents(documents)

        try:
            self.store.add_documents(docs)
        except Exception as e:
            print(f"Error adding_documents: {e}")

        return

    def search(self, query, agent_id):
        docs_with_score = self.store.max_marginal_relevance_search_with_score(query=query, filter={"agent_id": agent_id}, k=3)
        return docs_with_score      # list(int, Document(page_content, metadata))


vector_interface = VectorStoreInterface()
