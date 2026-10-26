# Azure upload rejected with HTTP 413

A 2.3 MB Excel file fits the backend's default 50 MiB file allowance, but the
root Docker image places Nginx in front of FastAPI. Its previous configuration
omitted `client_max_body_size`, leaving Nginx's default **1 MiB request limit**.
Local Vite/Uvicorn development bypasses that proxy, explaining the difference.
The screenshot's 413 is consistent with this configuration. The subsequent
job-status 404 was the browser checking whether an ambiguous upload succeeded;
when Nginx rejects the body, the API has not created a job for that request.

The root `nginx.conf` now sets:

```nginx
client_max_body_size 64m;
```

This permits the 50 MiB file plus multipart form fields and headers. The API
still enforces `MAX_UPLOAD_SIZE_MB` (default 50); the proxy change does not
permit arbitrarily large files. If you change the API allowance later, keep
every proxy/WAF limit above the total multipart request size. Do not change
`client_body_buffer_size`: it controls buffering, not the maximum accepted body.

The browser now shows a useful rejection message for HTTP 413, including the
API's actual file-limit message if it returns one. It does not poll job status
after explicit nontransient 4xx rejections. Recovery with the same request ID
continues for ambiguous network/timeouts, server failures and HTTP 408/429.

## Deploy the fix

1. Pull the updated `dev` branch on the machine that builds your Azure image.
2. Rebuild the **root Dockerfile image** and push a new version/tag to your
   existing container registry. The Dockerfile copies Nginx configuration into
   the image; updating source files or restarting the old image does not install
   the new configuration. Deploy the matching frontend change at the same time.
3. In Azure App Service's deployment/container settings, select that new image
   version and restart the app. Confirm its deployment/container logs show the
   new image. Use your established staging/maintenance procedure for active jobs.
4. In the application container's shell, the operator can inspect effective
   configuration with `nginx -T` and verify `client_max_body_size 64m;` in the
   server handling this app. This read-only command was not run during this fix.
5. In the permitted environment, upload the same 2.3 MB file. If 413 remains,
   inspect the effective deployed config and the rejecting proxy's logs. An
   additional gateway/WAF/custom startup config may impose a separate limit;
   the screenshot alone cannot identify another infrastructure layer.

The separate `frontend/Dockerfile` serves static files only; when it is used,
uploads go to its configured `VITE_API_BASE_URL`. Change the request limit on
the API-facing proxy actually receiving POST `/upload`, rather than increasing
a static server's limit that the upload never passes through. A built-in Azure
runtime also needs the override in its effective startup/server configuration;
editing a custom-image config has no effect if that image is not deployed.

This fix adds no migration and does not change AI, scoring, authentication,
worker settings or admin visibility. Prior release migrations remain required.
Verification was source syntax/import and Git diff review only. No tests,
builds, Nginx commands, application execution or live Azure/SQL calls were run.

References: [Nginx request body size](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size),
[Azure custom-container configuration](https://learn.microsoft.com/en-us/azure/app-service/configure-custom-container).
