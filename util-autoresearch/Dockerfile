FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fetch_papers.py .
COPY fetch_blog.py .
COPY zotero_save.py .
COPY tests ./tests

ENTRYPOINT ["python3", "fetch_papers.py"]
