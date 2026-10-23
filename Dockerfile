FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip uninstall -y pip \
    && useradd --system --uid 1000 --home /app app && mkdir -p data && chown app data
# --chown: o app (não-root) lê o código mesmo se os arquivos locais forem 600
COPY --chown=app:app . .
USER app
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080
EXPOSE 8080
# 401 = no ar (o painel exige senha); qualquer outra resposta ou erro = doente
HEALTHCHECK --interval=60s --timeout=5s CMD ["python", "-c", "import urllib.request as u, urllib.error as e, sys\ntry: u.urlopen('http://127.0.0.1:8080/', timeout=4)\nexcept e.HTTPError as r: sys.exit(r.code != 401)\nexcept Exception: sys.exit(1)"]
# banco (data/) no volume; log vai p/ stdout (docker logs)
CMD ["python", "main.py"]
