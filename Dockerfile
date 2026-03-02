FROM python:3.11-bullseye

# Evita prompts interativos durante instalação de pacotes
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Instala dependências do sistema (necessário para rede e SSL estável)
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    dnsutils \
    && rm -rf /var/lib/apt/lists/*

# Instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia todos os arquivos do projeto
COPY . .
# Porta que o Render vai expor para health check
EXPOSE 10000

CMD ["python", "main.py"]
