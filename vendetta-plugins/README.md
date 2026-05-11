# Vendetta plugins

Vendetta/Revenge plugins for Discord mobile. Currently:

- **[StealSticker](plugins/StealSticker/)** — steal stickers, modeled on [Stealmoji](https://aliernfrog.github.io/vd-plugins/Stealmoji/).

## Build

```bash
cd vendetta-plugins
npm install
npm run build
```

Outputs to `dist/<PluginName>/{index.js,manifest.json}`. Host the `dist/` directory on any static file server and install in Vendetta with the URL pointing at the plugin folder.
