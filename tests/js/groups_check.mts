// Проверка логики фильтров групп (web/src/lib/groups.ts): печатает JSON с результатами, их сверяет Python-тест.
import { inAnyGroup, inFilter, toggleFilter, NO_GROUP } from "../../web/src/lib/groups.ts";

const groups = [
  { id: "max", channels: ["A", "B", "C", "D"] },     // «Максим»
  { id: "stories", channels: ["C", "D", "E"] },       // «Рассказы»: C, D — и в «Максиме»
  { id: "lonely", channels: ["F"] },
];
const all = ["A", "B", "C", "D", "E", "F", "G"];       // G — без группы
const shown = (f: string[]) => all.filter((c) => inFilter(f, c, groups));
const union = (f: string[]) => all.filter((c) => inAnyGroup(f, c, groups));

console.log(JSON.stringify({
  none: shown([]),
  max: shown(["max"]),
  stories: shown(["stories"]),
  both: shown(["max", "stories"]),           // сужение: только те, что в обеих
  three: shown(["max", "stories", "lonely"]),
  noGroup: shown([NO_GROUP]),
  noGroupAndGroup: shown([NO_GROUP, "max"]),
  unionBoth: union(["max", "stories"]),      // для синхронизации — объединение
  unionNone: union([NO_GROUP]),
  unionMixed: union(["lonely", NO_GROUP]),
  unionEmpty: union([]),
  toggle: {
    add: toggleFilter(["max"], "stories"),
    remove: toggleFilter(["max", "stories"], "max"),
    noneClearsGroups: toggleFilter(["max", "stories"], NO_GROUP),
    groupClearsNone: toggleFilter([NO_GROUP], "max"),
  },
}));
