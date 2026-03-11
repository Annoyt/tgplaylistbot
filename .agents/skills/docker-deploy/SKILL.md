---
name: docker-deploy
description: Instructions for checking, managing, and redeploying the Docker and Nginx setup on the server for the tgplaylistbot project.
---

# Server Management & Docker Deployment

You are DevOps managing the `tgplaylistbot` deployment on the server (IP: 212.60.153.212).

## Architecture Context
- The app runs in a Docker container `musicbot-platform` bound to port `8000`.
- Nginx acts as a reverse proxy forwarding requests from port `80` to `127.0.0.1:8000`. 
- An external UI (`3x-ui`) runs on port `54321` and its stability must not be disturbed.
- All secrets are injected dynamically into `/tmp/deploy_env` by GitHub Actions prior to deployment, then moved to `.env`.

## Key Commands
- Check Docker logs: `docker logs -n 100 musicbot-platform`
- Check Docker status: `docker ps -a | grep musicbot`
- Check Nginx proxy: `cat /etc/nginx/sites-enabled/musicbot`
- Rebuild via SSH (manual):
  ```bash
  cd /root/tgplaylistbot
  docker compose down
  docker compose up -d --build
  ```

## Important Rules
1. Never modify or delete the `x-ui` or `3x-ui` configuration, container, or network port binding (54321).
2. Direct all web platform traffic through the Nginx reverse proxy.
3. Don't commit `.env` containing real values to source control.
