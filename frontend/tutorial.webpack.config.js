const path = require('path');
const webpack = require('webpack');
const TerserPlugin = require('terser-webpack-plugin');

// Shares the production renderer, but excludes events.ts and live transports.
module.exports = {
  mode: 'production',
  entry: './js/tutorial-player.js',
  output: {path: path.resolve(__dirname, '../backend/static/tutorial'), filename: 'player.js'},
  plugins: [new webpack.ProvidePlugin({$: 'jquery', jQuery: 'jquery'})],
  externals: {'popper.js': 'Popper'},
  optimization: {minimizer: [new TerserPlugin({extractComments: false})]},
  performance: {hints: false},
};
