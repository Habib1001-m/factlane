const docusaurusPreset = require('@docusaurus/babel/preset').default;
const transformRuntime = require.resolve('@babel/plugin-transform-runtime');

module.exports = function factlaneBabelConfig(api) {
  const config = docusaurusPreset(api);

  return {
    ...config,
    plugins: config.plugins.map((plugin) => {
      if (Array.isArray(plugin) && plugin[0] === transformRuntime) {
        return [plugin[0], {...plugin[1], absoluteRuntime: false}];
      }
      return plugin;
    }),
  };
};
