export class DistributionError extends Error {
  constructor(code, message, options) {
    super(message, options);
    this.name = "DistributionError";
    this.code = code;
  }
}
