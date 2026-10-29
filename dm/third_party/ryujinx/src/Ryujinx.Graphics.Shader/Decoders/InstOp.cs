using Ryujinx.Graphics.Shader.Instructions;

namespace Ryujinx.Graphics.Shader.Decoders
{
    readonly struct InstOp
    {
        public readonly ulong Address;
        public readonly ulong RawOpCode;
        // nxnative: the upper 64 bits of an Ampere (NVN2) instruction, which is 128 bits long (AmpereDecoder); 0 for Maxwell
        public readonly ulong RawOpCodeHi;
        public readonly InstEmitter Emitter;
        public readonly InstProps Props;
        public readonly InstName Name;

        public InstOp(ulong address, ulong rawOpCode, InstName name, InstEmitter emitter, InstProps props)
        {
            Address = address;
            RawOpCode = rawOpCode;
            RawOpCodeHi = 0;
            Name = name;
            Emitter = emitter;
            Props = props;
        }

        public InstOp(ulong address, ulong rawOpCode, ulong rawOpCodeHi, InstName name, InstEmitter emitter, InstProps props)
        {
            Address = address;
            RawOpCode = rawOpCode;
            RawOpCodeHi = rawOpCodeHi;
            Name = name;
            Emitter = emitter;
            Props = props;
        }

        public bool IsAmpere => Name >= InstName.AmpereFirst;

        public ulong GetAbsoluteAddress()
        {
            if (IsAmpere)
            {
                // Bits 32-81 are a signed byte offset from the next instruction. Instructions are 16 bytes, so its low 4 bits are not part of it: BRA keeps .U in bit 32
                long offset = (long)((RawOpCode >> 32) | (RawOpCodeHi << 32)) << 14 >> 14;

                return (ulong)((long)Address + 16 + (offset & ~15L));
            }

            return (ulong)((long)Address + (((int)(RawOpCode >> 20) << 8) >> 8) + 8);
        }
    }
}
