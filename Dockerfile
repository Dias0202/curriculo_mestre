# Utiliza a imagem oficial do Python otimizada e leve
FROM python:3.11-slim

# Define o diretório de trabalho no container
WORKDIR /app

# Copia os arquivos de dependência
COPY requirements.txt .

# Instala as dependências garantindo que o cache não ocupe espaço
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o código da aplicação para o container
COPY . .

# Expõe a porta 7860 exigida pelo Hugging Face Spaces para o health check
EXPOSE 7860

# Comando de inicialização
CMD ["python", "main.py"]
