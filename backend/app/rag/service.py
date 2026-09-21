"""
RAG (Retrieval-Augmented Generation) модуль для работы с документацией.
Обеспечивает индексацию документов и поиск релевантных фрагментов для LLM.
"""

import os
import hashlib
from typing import List, Dict, Any, Optional
from pathlib import Path

try:
    import chromadb
    from chromadb.config import Settings
    from sentence_transformers import SentenceTransformer
    import chardet
except ImportError as e:
    raise ImportError(
        "RAG dependencies not installed. Run: pip install chromadb sentence-transformers chardet"
    ) from e

from app.config import settings


class RAGService:
    """Сервис для управления векторным поиском по документации."""

    def __init__(self):
        self.enabled = getattr(settings, 'RAG_ENABLED', False)
        self.persist_dir = getattr(settings, 'CHROMA_PERSIST_DIR', './chroma_db')
        self.embedding_model_name = getattr(settings, 'EMBEDDING_MODEL', 'all-MiniLM-L6-v2')
        
        self.client = None
        self.collection = None
        self.embedder = None
        
        if self.enabled:
            self._initialize()

    def _initialize(self):
        """Инициализация ChromaDB и модели эмбеддингов."""
        try:
            # Инициализация клиента ChromaDB
            self.client = chromadb.PersistentClient(
                path=self.persist_dir,
                settings=Settings(anonymized_telemetry=False)
            )
            
            # Получение или создание коллекции
            self.collection = self.client.get_or_create_collection(
                name="documentation",
                metadata={"hnsw:space": "cosine"}
            )
            
            # Загрузка модели эмбеддингов
            self.embedder = SentenceTransformer(self.embedding_model_name)
            
            print(f"[RAG] Initialized with model: {self.embedding_model_name}, persist_dir: {self.persist_dir}")
        except Exception as e:
            print(f"[RAG] Initialization failed: {e}")
            self.enabled = False

    def _generate_embedding(self, text: str) -> List[float]:
        """Генерация векторного представления текста."""
        if not self.embedder:
            return []
        embeddings = self.embedder.encode(text, convert_to_numpy=True)
        return embeddings.tolist()

    def _detect_encoding(self, file_path: str) -> str:
        """Определение кодировки файла."""
        with open(file_path, 'rb') as f:
            result = chardet.detect(f.read(100000))  # Читаем первые 100KB
        return result.get('encoding', 'utf-8') or 'utf-8'

    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
        """Разбиение текста на чанки с перекрытием."""
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]
            
            # Пытаемся разбить по границе предложения или абзаца
            if end < len(text):
                for sep in ['.\n', '\n\n', '. ', ' ']:
                    last_sep = chunk.rfind(sep)
                    if last_sep > chunk_size // 2:
                        chunk = text[start:start + last_sep + len(sep)]
                        break
            
            chunks.append(chunk.strip())
            start += chunk_size - overlap
        
        return chunks

    def add_document(self, file_path: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Добавление документа в базу знаний."""
        if not self.enabled or not self.collection:
            return False

        try:
            path = Path(file_path)
            if not path.exists():
                print(f"[RAG] File not found: {file_path}")
                return False

            # Определение кодировки и чтение файла
            encoding = self._detect_encoding(str(path))
            with open(path, 'r', encoding=encoding, errors='ignore') as f:
                content = f.read()

            # Генерация уникального ID для документа
            doc_id = hashlib.md5(f"{path}:{len(content)}".encode()).hexdigest()
            
            # Разбиение на чанки
            chunks = self._chunk_text(content)
            
            if not chunks:
                print(f"[RAG] No chunks extracted from: {file_path}")
                return False

            # Подготовка данных для добавления
            ids = []
            documents = []
            metadatas = []
            embeddings = []

            for i, chunk in enumerate(chunks):
                chunk_id = f"{doc_id}_chunk_{i}"
                
                # Проверка на дублирование
                existing = self.collection.get(ids=[chunk_id])
                if existing and existing['ids']:
                    continue  # Пропускаем существующие чанки
                
                ids.append(chunk_id)
                documents.append(chunk)
                
                chunk_metadata = {
                    "source": str(path),
                    "filename": path.name,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    **(metadata or {})
                }
                metadatas.append(chunk_metadata)
                
                # Генерация эмбеддинга
                embedding = self._generate_embedding(chunk)
                if embedding:
                    embeddings.append(embedding)

            if ids and embeddings:
                self.collection.add(
                    ids=ids,
                    documents=documents,
                    metadatas=metadatas,
                    embeddings=embeddings
                )
                print(f"[RAG] Added {len(ids)} chunks from: {file_path}")
                return True
            else:
                print(f"[RAG] No new chunks to add from: {file_path}")
                return False

        except Exception as e:
            print(f"[RAG] Error adding document {file_path}: {e}")
            return False

    def search(self, query: str, n_results: int = 5) -> List[Dict[str, Any]]:
        """Поиск релевантных фрагментов документации."""
        if not self.enabled or not self.collection:
            return []

        try:
            query_embedding = self._generate_embedding(query)
            
            if not query_embedding:
                return []

            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["documents", "metadatas", "distances"]
            )

            if not results or not results['ids'] or not results['ids'][0]:
                return []

            formatted_results = []
            for i, doc_id in enumerate(results['ids'][0]):
                formatted_results.append({
                    "id": doc_id,
                    "content": results['documents'][0][i],
                    "metadata": results['metadatas'][0][i],
                    "distance": results['distances'][0][i] if results['distances'] else None
                })

            return formatted_results

        except Exception as e:
            print(f"[RAG] Search error: {e}")
            return []

    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики по базе знаний."""
        if not self.enabled or not self.collection:
            return {"enabled": False, "count": 0}

        try:
            count = self.collection.count()
            return {
                "enabled": True,
                "document_count": count,
                "collection_name": self.collection.name
            }
        except Exception as e:
            return {"enabled": False, "error": str(e)}

    def clear_collection(self):
        """Очистка всей коллекции документов."""
        if not self.enabled or not self.collection:
            return False
        
        try:
            self.client.delete_collection(self.collection.name)
            self.collection = self.client.get_or_create_collection(
                name="documentation",
                metadata={"hnsw:space": "cosine"}
            )
            print("[RAG] Collection cleared")
            return True
        except Exception as e:
            print(f"[RAG] Error clearing collection: {e}")
            return False


# Глобальный экземпляр сервиса
rag_service = RAGService()
