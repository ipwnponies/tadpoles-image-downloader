# Health Check

The Tadpoles downloader pings a health check URL after a successful upload.  

## Secret
To avoid storing that URL in plaintext, the repository includes an [age](https://age-encryption.org/) encrypted payload at
`secrets/healthcheck-url.age`.

The downloader looks for `$XDG_CONFIG_HOME/age/tadpoles-image-downloader.agekey` (derived with [`platformdirs`](https://pypi.org/project/platformdirs/)
so it resolves correctly on each OS). Copy or symlink the right identity file there on hosts that rely on the default.

## Rotating the secret

To generate a new identity and re-encrypt the URL:

```bash
age-keygen > $XDG_CONFIG_HOME/.config/tadpoles-age.key
printf '%s' '<healthcheck-url>' \
  | age --encrypt -r <your-public-key> \
  -o secrets/healthcheck_url.age
```

Supply the matching private key through one of the environment variables described above.
