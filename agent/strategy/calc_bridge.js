// Frozen @smogon/calc runs in an embedded JS context: no DOM, network, or callbacks.
function altruDistribution(damage) {
  if (typeof damage === 'number') return [[damage, 1]];
  if (!Array.isArray(damage) || !damage.length) return [[0, 1]];
  if (typeof damage[0] === 'number') {
    var counts = new Map();
    damage.forEach(function (d) { counts.set(d, (counts.get(d) || 0) + 1 / damage.length); });
    return Array.from(counts.entries());
  }
  var combined = new Map([[0, 1]]);
  damage.forEach(function (row) {
    var rolls = altruDistribution(row), next = new Map();
    combined.forEach(function (p, amount) {
      rolls.forEach(function (r) {
        var total = amount + r[0];
        next.set(total, (next.get(total) || 0) + p * r[1]);
      });
    });
    combined = next;
  });
  return Array.from(combined.entries());
}
function altruPokemon(packet) {
  var p = new calc.Pokemon(calc.Generations.get(9), packet.name, packet.options);
  // Current HP fraction is the public, consistently meaningful player-view field.
  p.originalCurHP = Math.max(1, Math.round(p.maxHP() * packet.hp_fraction));
  return p;
}
function altruCalculate(encoded) {
  var input = JSON.parse(encoded);
  var attacker = altruPokemon(input.attacker), defender = altruPokemon(input.defender);
  var move = new calc.Move(calc.Generations.get(9), input.move.name, input.move.options);
  var result = calc.calculate(calc.Generations.get(9), attacker, defender, move, new calc.Field(input.field));
  var distribution = altruDistribution(result.damage).sort(function (a, b) { return a[0] - b[0]; });
  return JSON.stringify({distribution: distribution, defender_max_hp: defender.maxHP(),
    defender_current_hp: defender.curHP(), min: distribution[0][0],
    max: distribution[distribution.length - 1][0], hits: result.move.hits});
}
