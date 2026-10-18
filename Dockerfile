FROM python:3.10-slim-bookworm

RUN apt-get update && \
    apt-get install -y git && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /requirements.txt

RUN pip install --upgrade pip
RUN pip install -r /requirements.txt

WORKDIR /Peter-File-Store
COPY . .

CMD ["python", "bot.py"]
