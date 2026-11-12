FROM python:3.11-slim AS builder

WORKDIR /build
COPY generate_site.py .
RUN python generate_site.py

FROM nginx:1.27-alpine
# 用同结构静态站点替换默认站点
RUN rm -rf /usr/share/nginx/html/*
COPY --from=builder /build/site/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
