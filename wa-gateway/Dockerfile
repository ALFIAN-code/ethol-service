FROM node:20-alpine
RUN apk add --no-cache git
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY index.js ./
ENV AUTH_DIR=/app/auth_info
EXPOSE 3000
CMD ["node", "index.js"]
