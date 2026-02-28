# Utiliza a imagem oficial do Python otimizada e leve
FROM python:3.11-slim

# Define o diretório de trabalho no container
WORKDIR /app

# Copia os arquivos de dependência primeiro para aproveitar cache do Docker
COPY requirements.txt .

# Instala as dependências
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o código para o container
COPY . .

# Executa a aplicação
CMD ["python", "main.py"]