# Dockerfile para BRN na Koyeb
FROM python:3.11-slim

# Instala dependências do sistema e o Tailscale
RUN apt-get update && apt-get install -y \
    curl \
    iptables \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Instala o Tailscale (binários estáticos)
COPY --from=tailscale/tailscale:latest /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=tailscale/tailscale:latest /usr/local/bin/tailscale /usr/local/bin/tailscale

# Define o diretório de trabalho
WORKDIR /app

# Copia os arquivos do projeto
COPY . .

# Instala as dependências do Python
RUN pip install --no-cache-dir -r requirements.txt

# Torna o script de inicialização executável
RUN chmod +x start.sh

# Expõe a porta que a Koyeb espera (ela injeta a variável $PORT)
EXPOSE 8000

# Comando de inicialização
CMD ["./start.sh"]