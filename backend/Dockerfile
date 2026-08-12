FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH=/app/src

# pip 镜像源: 默认清华 (国内部署稳定), 海外可 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

COPY src/ /app/src/

# 密钥/配置一律经 compose environment 注入 (LLM_API_KEY 等),
# 不 COPY .env 进镜像 — .env 不在 git 仓库, 且密钥进镜像层不安全

RUN mkdir -p storage/uploads storage/output storage/audit

EXPOSE 8010

CMD ["uvicorn", "dia.main:app", "--host", "0.0.0.0", "--port", "8010"]
