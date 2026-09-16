import Lean

open Lean

private def declarationKind (info : ConstantInfo) : String :=
  match info with
  | .thmInfo _ => "theorem"
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "def"
  | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive"
  | _ => ""

/-- Read declarations from an already built environment. This does not verify proofs. -/
unsafe def main (args : List String) : IO Unit := do
  if args.isEmpty then throw <| IO.userError "Specify at least one built module"
  initSearchPath (← findSysroot)
  enableInitializersExecution
  let opts := ({} : Options).setBool `pp.fullNames true
  let env ← importModules (args.toArray.map fun s => { module := s.toName }) opts
    (loadExts := true)
  let ctx : PPContext := { env, opts }
  let stdout ← IO.getStdout
  for (name, info) in env.constants.toList do
    let kind := declarationKind info
    if kind.isEmpty || name.isInternal || isPrivateName name then continue
    let some moduleIdx := env.getModuleIdxFor? name | continue
    let moduleName := env.header.moduleNames[moduleIdx.toNat]!
    let typeText ← ctx.runMetaM do return (← Meta.ppExpr info.type).pretty
    let ranges ← ctx.runCoreM <| findDeclarationRanges? name
    let doc := (← findDocString? env name).getD ""
    let line := ranges.map (·.range.pos.line) |>.getD 0
    stdout.putStrLn <| (Json.mkObj [
      ("full_name", toJson name.toString),
      ("kind", toJson kind),
      ("signature", toJson typeText),
      ("docstring", toJson doc),
      ("module", toJson moduleName.toString),
      ("line", toJson line)
    ]).compress
