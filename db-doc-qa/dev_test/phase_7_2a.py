import numpy as np

from data_agent import doc_ingest
from data_agent.config import load_config

# Needs: `ollama pull nomic-embed-text` and a running Ollama server.
emb = load_config("configs/eval/hybridqa_dev50_ollama.yaml").agent.embedding
docs = doc_ingest.embed(["Ann Lee was born in Paris.", "The stadium holds 40,000 people."], "RETRIEVAL_DOCUMENT", emb)
query = doc_ingest.embed(["Where was Ann Lee born?"], "RETRIEVAL_QUERY", emb)
print("provider/model:", emb.provider, emb.model)
print("vector size:", None if docs is None else docs.shape)
if docs is not None and query is not None:
    print("query vs doc 1 / doc 2 cosine:", np.round(docs @ query[0], 3))
