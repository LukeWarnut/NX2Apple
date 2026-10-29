namespace Ryujinx.Graphics.Shader.Decoders
{
    static class RegisterConsts
    {
        public const int GprsCount = 255;
        public const int PredsCount = 7;
        public const int FlagsCount = 4;
        public const int TotalCount = GprsCount + PredsCount + FlagsCount;

        // nxnative: the SSA key space. Ampere (NVN2) code keeps its uniform predicates UP0-UP6 as predicates 8-14 and its uniform registers UR0-UR62 as flags 0-62
        // (Maxwell's condition-code flags, which Ampere does not have; see AmpereDecoder), so both take more keys than Maxwell's counts
        public const int PredKeysCount = 16;
        public const int FlagKeysCount = 64;
        public const int TotalKeysCount = GprsCount + PredKeysCount + FlagKeysCount;

        public const int RegisterZeroIndex = GprsCount;
        public const int PredicateTrueIndex = PredsCount;
    }
}
