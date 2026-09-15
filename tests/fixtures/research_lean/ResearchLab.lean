import Std

namespace ResearchLab

/-- Every natural number is unchanged by adding zero. -/
theorem addition_identity (n : Nat) : n + 0 = n := Nat.add_zero n

end ResearchLab
