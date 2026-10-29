using Ryujinx.Graphics.Shader.Instructions;
using Ryujinx.Graphics.Shader.Translation;
using System;
using System.Collections.Generic;

namespace Ryujinx.Graphics.Shader.Decoders
{
    // nxnative: decoder for Ampere (Switch 2 NVN2) machine code, which Ryujinx does not have. Each instruction is 128 bits with its scheduling control in the top bits
    // (105-125, not read here); bits 0-11 are the opcode, 12-14 the guard predicate with 15 inverting it. ALU instructions keep their opcode in bits 0-8 and the form of their
    // second and third operands in bits 9-11 (AmpInst). The encodings follow Mesa's NAK (src/nouveau/compiler/nak/sm70_encode.rs) and, for instructions NAK does not emit
    // (FSEL, I2FP, P2R / R2P, CALL / RET, BRX, DEPBAR), CuAssembler's SM86 instruction repository; the opcodes are those of every shader in Donkey Kong Bananza.
    // The output is the same DecodedProgram the Maxwell decoder builds, so everything after decoding is shared. The convergence instructions (BSSY, BSYNC, BREAK, WARPSYNC)
    // order a warp, not one thread, so they do nothing here, the same as Maxwell's SSY / SYNC once their targets are known; CALL / RET become functions.
    static class AmpereDecoder
    {
        // The code area of a graphics program: the 0x50-byte SPH (the Maxwell layout, version 4), zeros to 0x80, then the instructions. A compute program starts at +0.
        public const int HeaderSize = 0x80;

        private const int InstSize = 16;

        private readonly struct TableEntry
        {
            public readonly InstName Name;
            public readonly InstEmitter Emitter;
            public readonly InstProps Props;

            public TableEntry(InstName name, InstEmitter emitter, InstProps props)
            {
                Name = name;
                Emitter = emitter;
                Props = props;
            }
        }

        private static readonly Dictionary<int, TableEntry> _table = BuildTable();

        private static Dictionary<int, TableEntry> BuildTable()
        {
            Dictionary<int, TableEntry> t = new();

            // ALU forms: 1 = Rb, 2 = Rc and imm32, 3 = Rc and cbuf, 4 = imm32, 5 = cbuf, 6 = UR, 7 = Rc and UR (AmpInst.SrcB / SrcC)
            void Alu(int op9, InstEmitter emitter, InstProps props = InstProps.Rd)
            {
                for (int form = 1; form <= 7; form++)
                {
                    t[op9 | (form << 9)] = new TableEntry(InstName.AmpereAlu, emitter, props);
                }
            }

            void Op(int op12, InstName name, InstEmitter emitter, InstProps props = InstProps.None)
            {
                t[op12] = new TableEntry(name, emitter, props);
            }

            Alu(0x002, InstEmit.AmpMov);
            Alu(0x007, InstEmit.AmpSel);
            Alu(0x008, InstEmit.AmpFsel);
            Alu(0x009, InstEmit.AmpFmnmx);
            Alu(0x00a, InstEmit.AmpFset);
            Alu(0x00b, InstEmit.AmpFsetp, InstProps.None);
            Alu(0x00c, InstEmit.AmpIsetp, InstProps.None);
            Alu(0x010, InstEmit.AmpIadd3);
            Alu(0x011, InstEmit.AmpLea);
            Alu(0x012, InstEmit.AmpLop3);
            Alu(0x013, InstEmit.AmpIabs);
            Alu(0x016, InstEmit.AmpPrmt);
            Alu(0x017, InstEmit.AmpImnmx);
            Alu(0x019, InstEmit.AmpShf);
            Alu(0x01a, InstEmit.AmpSgxt);
            Alu(0x01b, InstEmit.AmpBmsk);
            Alu(0x020, InstEmit.AmpFmul);
            Alu(0x021, InstEmit.AmpFadd);
            Alu(0x023, InstEmit.AmpFfma);
            Alu(0x024, InstEmit.AmpImad);
            Alu(0x025, InstEmit.AmpImadWide);
            Alu(0x027, InstEmit.AmpImadHi);
            Alu(0x030, InstEmit.AmpHadd2);
            Alu(0x031, InstEmit.AmpHfma2);
            Alu(0x032, InstEmit.AmpHmul2);
            Alu(0x03e, InstEmit.AmpF2fp);
            Alu(0x045, InstEmit.AmpI2fp);
            Alu(0x100, InstEmit.AmpFlo);
            Alu(0x101, InstEmit.AmpBrev);
            Alu(0x104, InstEmit.AmpF2f);
            Alu(0x105, InstEmit.AmpF2i);
            Alu(0x106, InstEmit.AmpI2f);
            Alu(0x107, InstEmit.AmpFrnd);
            Alu(0x108, InstEmit.AmpMufu);
            Alu(0x109, InstEmit.AmpPopc);
            Alu(0x124, InstEmit.AmpOut);
            Alu(0x148, InstEmit.AmpNop, InstProps.None);   // WARPSYNC

            // Uniform datapath: the same forms with uniform registers (AmpInst.USrcB / USrcC); the destination is a UR
            Alu(0x082, InstEmit.AmpUmov, InstProps.None);
            Alu(0x087, InstEmit.AmpUsel, InstProps.None);
            Alu(0x08c, InstEmit.AmpUisetp, InstProps.None);
            Alu(0x090, InstEmit.AmpUiadd3, InstProps.None);
            Alu(0x091, InstEmit.AmpUlea, InstProps.None);
            Alu(0x092, InstEmit.AmpUlop3, InstProps.None);
            Alu(0x096, InstEmit.AmpUprmt, InstProps.None);
            Alu(0x099, InstEmit.AmpUshf, InstProps.None);
            Alu(0x09a, InstEmit.AmpUsgxt, InstProps.None);
            Alu(0x0a4, InstEmit.AmpUimad, InstProps.None);
            Alu(0x0a5, InstEmit.AmpUimadWide, InstProps.None);
            Alu(0x0bd, InstEmit.AmpUflo, InstProps.None);
            Alu(0x0be, InstEmit.AmpUbrev, InstProps.None);
            Alu(0x0bf, InstEmit.AmpUpopc, InstProps.None);

            Op(0x803, InstName.AmpereAlu, InstEmit.AmpP2r, InstProps.Rd);
            Op(0x804, InstName.AmpereAlu, InstEmit.AmpR2p);
            Op(0x805, InstName.AmpereAlu, InstEmit.AmpCs2r, InstProps.Rd);
            Op(0x806, InstName.AmpereAlu, InstEmit.AmpVote, InstProps.Rd);
            Op(0x886, InstName.AmpereAlu, InstEmit.AmpVoteu);
            Op(0x81c, InstName.AmpereAlu, InstEmit.AmpPlop3);
            Op(0x89c, InstName.AmpereAlu, InstEmit.AmpUplop3);
            Op(0x822, InstName.AmpereAlu, InstEmit.AmpFswzadd, InstProps.Rd);
            Op(0x919, InstName.AmpereAlu, InstEmit.AmpS2r, InstProps.Rd);
            Op(0x9c3, InstName.AmpereAlu, InstEmit.AmpS2ur);
            Op(0x3c2, InstName.AmpereAlu, InstEmit.AmpR2ur);
            Op(0x34e, InstName.AmpereAlu, InstEmit.AmpLepc, InstProps.Rd);
            Op(0x389, InstName.AmpereAlu, InstEmit.AmpShfl, InstProps.Rd);
            Op(0x589, InstName.AmpereAlu, InstEmit.AmpShfl, InstProps.Rd);
            Op(0x989, InstName.AmpereAlu, InstEmit.AmpShfl, InstProps.Rd);
            Op(0xf89, InstName.AmpereAlu, InstEmit.AmpShfl, InstProps.Rd);

            Op(0xab9, InstName.AmpereAlu, InstEmit.AmpUldc);
            Op(0xabb, InstName.AmpereAlu, InstEmit.AmpUldc);
            Op(0xb82, InstName.AmpereAlu, InstEmit.AmpLdc, InstProps.Rd);
            Op(0x582, InstName.AmpereAlu, InstEmit.AmpLdc, InstProps.Rd);
            Op(0x981, InstName.AmpereAlu, InstEmit.AmpLdg, InstProps.Rd);
            Op(0x381, InstName.AmpereAlu, InstEmit.AmpLdg, InstProps.Rd);
            Op(0x986, InstName.AmpereAlu, InstEmit.AmpStg);
            Op(0x386, InstName.AmpereAlu, InstEmit.AmpStg);
            Op(0x983, InstName.AmpereAlu, InstEmit.AmpLdl, InstProps.Rd);
            Op(0x987, InstName.AmpereAlu, InstEmit.AmpStl);
            Op(0x387, InstName.AmpereAlu, InstEmit.AmpStl);
            Op(0x984, InstName.AmpereAlu, InstEmit.AmpLds, InstProps.Rd);
            Op(0x988, InstName.AmpereAlu, InstEmit.AmpSts);
            Op(0x388, InstName.AmpereAlu, InstEmit.AmpSts);
            Op(0x9a8, InstName.AmpereAlu, InstEmit.AmpAtomg, InstProps.Rd);
            Op(0x3a8, InstName.AmpereAlu, InstEmit.AmpAtomg, InstProps.Rd);
            Op(0x3a9, InstName.AmpereAlu, InstEmit.AmpAtomgCas, InstProps.Rd);
            Op(0x98e, InstName.AmpereAlu, InstEmit.AmpRed);
            Op(0x38c, InstName.AmpereAlu, InstEmit.AmpAtoms, InstProps.Rd);
            Op(0x98c, InstName.AmpereAlu, InstEmit.AmpAtoms, InstProps.Rd);
            Op(0xb1d, InstName.AmpereAlu, InstEmit.AmpBar);
            Op(0x992, InstName.AmpereAlu, InstEmit.AmpMembar);

            Op(0x321, InstName.AmpereAlu, InstEmit.AmpAld, InstProps.Rd);
            Op(0x322, InstName.AmpereAlu, InstEmit.AmpAst);
            Op(0x326, InstName.AmpereAlu, InstEmit.AmpIpa, InstProps.Rd);
            Op(0x920, InstName.AmpereAlu, InstEmit.AmpAl2p, InstProps.Rd);
            Op(0x923, InstName.AmpereAlu, InstEmit.AmpIsberd, InstProps.Rd);
            Op(0x925, InstName.AmpereAlu, InstEmit.AmpPixld, InstProps.Rd);

            // Bound (handle in a constant buffer) and bindless (handle in a register) forms
            Op(0xb60, InstName.AmpereAlu, InstEmit.AmpTex, InstProps.Rd | InstProps.Tex);
            Op(0x361, InstName.AmpereAlu, InstEmit.AmpTex, InstProps.Rd | InstProps.TexB);
            Op(0xb66, InstName.AmpereAlu, InstEmit.AmpTld, InstProps.Rd | InstProps.Tex);
            Op(0x367, InstName.AmpereAlu, InstEmit.AmpTld, InstProps.Rd | InstProps.TexB);
            Op(0xb63, InstName.AmpereAlu, InstEmit.AmpTld4, InstProps.Rd | InstProps.Tex);
            Op(0x364, InstName.AmpereAlu, InstEmit.AmpTld4, InstProps.Rd | InstProps.TexB);
            Op(0xb69, InstName.AmpereAlu, InstEmit.AmpTmml, InstProps.Rd | InstProps.Tex);
            Op(0x36a, InstName.AmpereAlu, InstEmit.AmpTmml, InstProps.Rd | InstProps.TexB);
            Op(0xb6c, InstName.AmpereAlu, InstEmit.AmpTxd, InstProps.Rd | InstProps.Tex);
            Op(0x36d, InstName.AmpereAlu, InstEmit.AmpTxd, InstProps.Rd | InstProps.TexB);
            Op(0xb6f, InstName.AmpereAlu, InstEmit.AmpTxq, InstProps.Rd | InstProps.Tex);
            Op(0x370, InstName.AmpereAlu, InstEmit.AmpTxq, InstProps.Rd | InstProps.TexB);

            Op(0x947, InstName.AmpereBra, InstEmit.AmpBra, InstProps.Bra | InstProps.NoPred);
            Op(0x949, InstName.AmpereBrx, InstEmit.AmpBrx, InstProps.Bra | InstProps.NoPred);
            Op(0x958, InstName.AmpereBrx, InstEmit.AmpBrx, InstProps.Bra | InstProps.NoPred);
            Op(0x944, InstName.AmpereCall, InstEmit.AmpCall, InstProps.Bra);
            Op(0x950, InstName.AmpereRet, InstEmit.AmpRet, InstProps.Bra | InstProps.NoPred);
            Op(0x94d, InstName.AmpereExit, InstEmit.AmpExit, InstProps.Bra | InstProps.NoPred);
            Op(0x95b, InstName.AmpereAlu, InstEmit.AmpKill, InstProps.Bra);   // ends its block, as Maxwell's KIL: an unconditional one has no successor
            Op(0x918, InstName.AmpereAlu, InstEmit.AmpNop);   // NOP
            Op(0x91a, InstName.AmpereAlu, InstEmit.AmpNop);   // DEPBAR: waits for scoreboards
            Op(0x945, InstName.AmpereAlu, InstEmit.AmpNop);   // BSSY
            Op(0x941, InstName.AmpereAlu, InstEmit.AmpNop);   // BSYNC
            Op(0x942, InstName.AmpereAlu, InstEmit.AmpNop);   // BREAK
            Op(0x946, InstName.AmpereAlu, InstEmit.AmpNop);   // YIELD
            Op(0x98f, InstName.AmpereAlu, InstEmit.AmpNop);   // CCTL: cache control
            // Geometry shader prologue (Bananza's 4 geometry shaders: LEPC, an address 0x770 bytes on, then 0x357, and 0x957 later): not identified, nothing in a thread's results reads them
            Op(0x357, InstName.AmpereAlu, InstEmit.AmpNop);
            Op(0x957, InstName.AmpereAlu, InstEmit.AmpNop);

            return t;
        }

        public static InstOp GetOp(ulong address, ulong lo, ulong hi)
        {
            if (_table.TryGetValue((int)(lo & 0xfff), out TableEntry e))
            {
                return new InstOp(address, lo, hi, e.Name, e.Emitter, e.Props);
            }

            return new InstOp(address, lo, hi, InstName.AmpereAlu, InstEmit.AmpUnknown, InstProps.None);
        }

        // The guard (bits 12-15) and, for BRA / BRX / EXIT / RET, the condition predicate (bits 87-90) are both PT
        public static bool IsUnconditional(InstOp op)
        {
            AmpInst inst = new(op);

            if (inst.GuardPred != RegisterConsts.PredicateTrueIndex || inst.GuardInv)
            {
                return false;
            }

            if (op.Name is InstName.AmpereBra or InstName.AmpereBrx or InstName.AmpereExit or InstName.AmpereRet)
            {
                return inst.Bits(87, 3) == RegisterConsts.PredicateTrueIndex && !inst.Bit(90);
            }

            // CALL returns to the next instruction
            return op.Name != InstName.AmpereCall;
        }

        public static DecodedProgram Decode(ShaderDefinitions definitions, IGpuAccessor gpuAccessor, ulong startAddress)
        {
            Decoder.Context context = new(gpuAccessor);
            Queue<DecodedFunction> functionsQueue = new();
            Dictionary<ulong, DecodedFunction> functionsVisited = new();

            DecodedFunction EnqueueFunction(ulong address)
            {
                if (!functionsVisited.TryGetValue(address, out DecodedFunction function))
                {
                    functionsVisited.Add(address, function = new DecodedFunction(address));
                    functionsQueue.Enqueue(function);
                }

                return function;
            }

            DecodedFunction mainFunction = EnqueueFunction(0);

            while (functionsQueue.TryDequeue(out DecodedFunction currentFunction))
            {
                List<Block> blocks = [];
                Queue<Block> workQueue = new();
                Dictionary<ulong, Block> visited = new();

                Block GetBlock(ulong blkAddress)
                {
                    if (!visited.TryGetValue(blkAddress, out Block block))
                    {
                        block = new Block(blkAddress);

                        workQueue.Enqueue(block);
                        visited.Add(blkAddress, block);
                    }

                    return block;
                }

                GetBlock(currentFunction.Address);

                bool hasNewTarget;

                do
                {
                    while (workQueue.TryDequeue(out Block currBlock))
                    {
                        // The same block discovery as Decoder.Decode, without the SSY / PBK stacks
                        if (BinarySearch(blocks, currBlock.Address, out int nBlkIndex))
                        {
                            Block nBlock = blocks[nBlkIndex];

                            if (nBlock.Address == currBlock.Address)
                            {
                                throw new InvalidOperationException("Found duplicate block address on the list.");
                            }

                            nBlock.Split(currBlock);
                            blocks.Insert(nBlkIndex + 1, currBlock);

                            continue;
                        }

                        ulong limitAddress = ulong.MaxValue;

                        if (nBlkIndex != blocks.Count)
                        {
                            Block nBlock = blocks[nBlkIndex];

                            int nextIndex = nBlkIndex + 1;

                            if (nBlock.Address < currBlock.Address && nextIndex < blocks.Count)
                            {
                                limitAddress = blocks[nextIndex].Address;
                            }
                            else if (nBlock.Address > currBlock.Address)
                            {
                                limitAddress = blocks[nBlkIndex].Address;
                            }
                        }

                        FillBlock(definitions, gpuAccessor, context, currBlock, limitAddress, startAddress);

                        if (currBlock.OpCodes.Count != 0)
                        {
                            InstOp lastOp = currBlock.GetLastOp();

                            if (lastOp.Name == InstName.AmpereCall)
                            {
                                EnqueueFunction(lastOp.GetAbsoluteAddress()).AddCaller(currentFunction);
                            }
                            else if (lastOp.Name == InstName.AmpereBra)
                            {
                                Block succBlock = GetBlock(lastOp.GetAbsoluteAddress());
                                currBlock.Successors.Add(succBlock);
                                succBlock.Predecessors.Add(currBlock);
                            }

                            if (!Decoder.IsUnconditionalBranch(ref lastOp))
                            {
                                Block succBlock = GetBlock(currBlock.EndAddress);
                                currBlock.Successors.Insert(0, succBlock);
                                succBlock.Predecessors.Add(currBlock);
                            }
                        }

                        if (blocks.Count != 0)
                        {
                            Block nBlock = blocks[nBlkIndex];

                            blocks.Insert(nBlkIndex + (nBlock.Address < currBlock.Address ? 1 : 0), currBlock);
                        }
                        else
                        {
                            blocks.Add(currBlock);
                        }
                    }

                    hasNewTarget = FindBrxTargets(context, gpuAccessor, blocks, GetBlock);
                }
                while (hasNewTarget);

                currentFunction.SetBlocks(blocks.ToArray());
            }

            return new DecodedProgram(
                mainFunction,
                functionsVisited,
                context.AttributeUsage,
                context.UsedFeatures,
                context.ClipDistancesWritten,
                context.Cb1DataSize);
        }

        private static bool BinarySearch(List<Block> blocks, ulong address, out int index)
        {
            index = 0;

            int left = 0;
            int right = blocks.Count - 1;

            while (left <= right)
            {
                int size = right - left;

                int middle = left + (size >> 1);

                Block block = blocks[middle];

                index = middle;

                if (address >= block.Address && address < block.EndAddress)
                {
                    return true;
                }

                if (address < block.Address)
                {
                    right = middle - 1;
                }
                else
                {
                    left = middle + 1;
                }
            }

            return false;
        }

        private static void FillBlock(
            ShaderDefinitions definitions,
            IGpuAccessor gpuAccessor,
            Decoder.Context context,
            Block block,
            ulong limitAddress,
            ulong startAddress)
        {
            ulong address = block.Address;

            InstOp op = default;

            do
            {
                if (address + InstSize - 1 >= limitAddress)
                {
                    break;
                }

                ReadOnlySpan<ulong> buffer = gpuAccessor.GetCode(startAddress + address, InstSize);

                op = GetOp(address, buffer[0], buffer[1]);

                // CALL.REL.NOINC keeps no return address: a function returns with RET.REL.NODEC through a register the caller set. The compiler also uses it as a jump,
                // to leave a loop for code that ends in EXIT (Donkey Kong Bananza's agl shaders: "@P0 CALL" then a BRA back to the loop's head). Made a function, that EXIT
                // was dropped ("Invalid exit on non-main function") and the loop never ended, a GPU fault. A CALL whose target reaches no RET is a branch.
                if (op.Name == InstName.AmpereCall && !CalleeReturns(gpuAccessor, startAddress, op.GetAbsoluteAddress()))
                {
                    op = new InstOp(address, buffer[0], buffer[1], InstName.AmpereBra, InstEmit.AmpBra, InstProps.Bra | InstProps.NoPred);
                }

                AmpInst inst = new(op);

                if (op.Props.HasFlag(InstProps.TexB))
                {
                    context.SetUsedFeature(FeatureFlags.Bindless);
                }

                switch (inst.Op12)
                {
                    case 0x321: // ALD
                        Decoder.SetUserAttributeUses(definitions, context, inst.Bits(40, 10), inst.Bits(74, 2) + 1, inst.Bit(79), inst.Bit(77), inst.Bit(76));
                        break;
                    case 0x322: // AST
                        Decoder.SetUserAttributeUses(definitions, context, inst.Bits(40, 10), inst.Bits(74, 2) + 1, true, inst.Bit(77), inst.Bit(76));
                        break;
                    case 0x326: // IPA
                        Decoder.SetUserAttributeUses(definitions, context, inst.Bits(64, 8) * 4, 1, false, false, false);
                        break;
                    case 0x389:
                    case 0x589:
                    case 0x989:
                    case 0xf89:
                    case 0x806:
                    case 0x886:
                        context.SetUsedFeature(FeatureFlags.Shuffle);
                        break;
                    case 0x983:
                    case 0x987:
                    case 0x387:
                        context.SetUsedFeature(FeatureFlags.LocalMemory);
                        break;
                    case 0x984:
                    case 0x988:
                    case 0x388:
                    case 0x38c:
                    case 0x98c:
                        context.SetUsedFeature(FeatureFlags.SharedMemory);
                        break;
                    case 0x986:
                    case 0x386:
                    case 0x9a8:
                    case 0x3a8:
                    case 0x3a9:
                    case 0x98e:
                        context.SetUsedFeature(FeatureFlags.Store);
                        break;
                }

                block.OpCodes.Add(op);

                address += InstSize;
            }
            while (!op.Props.HasFlag(InstProps.Bra));

            block.EndAddress = address;
        }

        // Whether code entered at target can reach a RET, following branches (both ways of a conditional one) and stepping over calls; a BRX counts as reaching one,
        // as its targets are not known here. Paths end at an unconditional EXIT or BRA, or at a run of zero words (past the end of the code)
        private static bool CalleeReturns(IGpuAccessor gpuAccessor, ulong startAddress, ulong target)
        {
            HashSet<ulong> visited = new();
            Stack<ulong> pending = new();
            pending.Push(target);

            while (pending.Count != 0 && visited.Count < 0x10000)
            {
                ulong address = pending.Pop();

                while (visited.Add(address))
                {
                    ReadOnlySpan<ulong> buffer = gpuAccessor.GetCode(startAddress + address, InstSize);

                    if (buffer[0] == 0 && buffer[1] == 0)
                    {
                        break;
                    }

                    InstOp op = GetOp(address, buffer[0], buffer[1]);

                    if (op.Name is InstName.AmpereRet or InstName.AmpereBrx)
                    {
                        return true;
                    }

                    if (op.Name == InstName.AmpereBra)
                    {
                        pending.Push(op.GetAbsoluteAddress());
                    }

                    if ((op.Name is InstName.AmpereBra or InstName.AmpereExit) && IsUnconditional(op))
                    {
                        break;
                    }

                    address += InstSize;
                }
            }

            return false;
        }

        private static bool FindBrxTargets(Decoder.Context context, IGpuAccessor gpuAccessor, IEnumerable<Block> blocks, Func<ulong, Block> getBlock)
        {
            bool hasNewTarget = false;

            foreach (Block block in blocks)
            {
                InstOp lastOp = block.GetLastOp();
                bool hasNext = block.HasNext();

                if (lastOp.Name == InstName.AmpereBrx && block.Successors.Count == (hasNext ? 1 : 0))
                {
                    HashSet<ulong> visited = [];

                    // BRX Ra, imm: the target is Ra + the address of the next instruction + imm (InstOp.GetAbsoluteAddress)
                    ulong baseOffset = lastOp.GetAbsoluteAddress();

                    AmpInst brx = new(lastOp);
                    (int cbBaseOffset, int cbOffsetsCount) = brx.Op12 == 0x958 ? FindBrxuTargetRange(block, brx.URa) : FindBrxTargetRange(block, brx.Ra);

                    if (cbOffsetsCount == 0)
                    {
                        gpuAccessor.Log($"Ampere BRX at 0x{lastOp.Address:X}: its jump table was not found (IMNMX.U32 or USEL, a shift by 2, LDC or ULDC c[0x1])");
                        continue;
                    }

                    hasNewTarget = true;

                    for (int i = 0; i < cbOffsetsCount; i++)
                    {
                        uint targetOffset = context.ConstantBuffer1Read(cbBaseOffset + i * 4);
                        ulong targetAddress = baseOffset + targetOffset;

                        if (visited.Add(targetAddress))
                        {
                            Block target = getBlock(targetAddress);
                            target.Predecessors.Add(block);
                            block.Successors.Add(target);
                        }
                    }
                }
            }

            return hasNewTarget;
        }

        private static (int, int) FindBrxTargetRange(Block block, int brxReg)
        {
            // The switch tables of Bananza's shaders (the same shape as Maxwell's, see Decoder.FindBrxTargetRange):
            //
            // IMNMX.U32 Rx, Rx, UpperBound, PT
            // SHF.L.U32 Ry, Rx, 0x2, RZ          (or IMAD.SHL / IMAD Ry, Rx, 0x4, RZ, or LEA Ry, Rx, RZ, 0x2)
            // LDC Rz, c[0x1][Ry+BaseOffset]
            // BRX Rz, imm
            HashSet<Block> visited = [];

            (Block ldcBlock, int ldcIndex) = FindFirstRegWrite(visited, block, block.OpCodes.Count - 1, brxReg);
            if (ldcBlock == null)
            {
                return (0, 0);
            }

            AmpInst ldc = new(ldcBlock.OpCodes[ldcIndex]);

            if (ldc.Op12 != 0xb82 || ldc.Bits(54, 5) != 1 || ldc.Bits(78, 2) != 0 || ldc.Bits(73, 3) != 4)
            {
                return (0, 0);
            }

            (Block shlBlock, int shlIndex) = FindFirstRegWrite(visited, ldcBlock, ldcIndex, ldc.Ra);
            if (shlBlock == null)
            {
                return (0, 0);
            }

            AmpInst shl = new(shlBlock.OpCodes[shlIndex]);
            int indexReg;

            if (shl.Op9 == 0x019 && shl.Form == 4 && !shl.Bit(76) && shl.Imm32 == 2 && shl.Rc == RegisterConsts.RegisterZeroIndex)
            {
                indexReg = shl.Ra;   // SHF.L
            }
            else if (shl.Op9 == 0x024 && shl.Form == 4 && shl.Imm32 == 4 && shl.Rc == RegisterConsts.RegisterZeroIndex)
            {
                indexReg = shl.Ra;   // IMAD.SHL.U32
            }
            else if (shl.Op9 == 0x011 && shl.Form == 1 && !shl.Bit(80) && shl.Bits(75, 5) == 2 && shl.Rb == RegisterConsts.RegisterZeroIndex)
            {
                indexReg = shl.Ra;   // LEA
            }
            else
            {
                return (0, 0);
            }

            (Block mnmxBlock, int mnmxIndex) = FindFirstRegWrite(visited, shlBlock, shlIndex, indexReg);
            if (mnmxBlock == null)
            {
                return (0, 0);
            }

            AmpInst mnmx = new(mnmxBlock.OpCodes[mnmxIndex]);

            // IMNMX.U32 with PT: the minimum of the index and the bound
            if (mnmx.Op9 != 0x017 || mnmx.Form != 4 || mnmx.Bit(73) || mnmx.Bits(87, 3) != RegisterConsts.PredicateTrueIndex || mnmx.Bit(90))
            {
                return (0, 0);
            }

            return (ldc.Bits(38, 16), (int)mnmx.Imm32 + 1);
        }

        private static (int, int) FindBrxuTargetRange(Block block, int brxReg)
        {
            // BRXU takes a UR, and the index is computed in the uniform datapath (2 pixel shaders in Bananza's layouts):
            //
            // UISETP.LT UPn, URx, UpperBound  (not checked: USEL keeps URx while it is below the bound)
            // USEL URy, URx, UpperBound, UPn
            // USHF.L.U32 URz, URy, 0x2, URZ    (or UIMAD URz, URy, 0x4, URZ)
            // ULDC URw, c[0x1][URz + BaseOffset]
            // BRXU URw, imm
            HashSet<Block> visited = [];

            (Block ldcBlock, int ldcIndex) = FindFirstUniformWrite(visited, block, block.OpCodes.Count - 1, brxReg);
            if (ldcBlock == null)
            {
                return (0, 0);
            }

            AmpInst ldc = new(ldcBlock.OpCodes[ldcIndex]);

            if (ldc.Op12 != 0xabb || ldc.Bits(54, 5) != 1 || ldc.Bits(73, 3) != 4)   // bit 91 is set on every uniform instruction
            {
                return (0, 0);
            }

            (Block shlBlock, int shlIndex) = FindFirstUniformWrite(visited, ldcBlock, ldcIndex, ldc.URa);
            if (shlBlock == null)
            {
                return (0, 0);
            }

            AmpInst shl = new(shlBlock.OpCodes[shlIndex]);

            bool isShl = shl.Op9 == 0x099 && shl.Form == 4 && !shl.Bit(76) && shl.Imm32 == 2 && shl.URc == 63;
            bool isMul = shl.Op9 == 0x0a4 && shl.Form == 4 && shl.Imm32 == 4 && shl.URc == 63;

            if (!isShl && !isMul)
            {
                return (0, 0);
            }

            (Block selBlock, int selIndex) = FindFirstUniformWrite(visited, shlBlock, shlIndex, shl.URa);
            if (selBlock == null)
            {
                return (0, 0);
            }

            AmpInst sel = new(selBlock.OpCodes[selIndex]);

            if (sel.Op9 != 0x087 || sel.Form != 4 || (int)sel.Imm32 < 0 || sel.Imm32 > 0x1000)
            {
                return (0, 0);
            }

            return (ldc.Bits(38, 16), (int)sel.Imm32 + 1);
        }

        private static (Block, int) FindFirstRegWrite(HashSet<Block> visited, Block startBlock, int startIndex, int regIndex)
        {
            return FindFirstWrite(visited, startBlock, startIndex, op => WritesToRegister(op, regIndex));
        }

        private static (Block, int) FindFirstUniformWrite(HashSet<Block> visited, Block startBlock, int startIndex, int urIndex)
        {
            return FindFirstWrite(visited, startBlock, startIndex, op => WritesToUniformRegister(op, urIndex));
        }

        private static (Block, int) FindFirstWrite(HashSet<Block> visited, Block startBlock, int startIndex, Func<InstOp, bool> writes)
        {
            Queue<(Block, int)> toVisit = new();
            toVisit.Enqueue((startBlock, startIndex));
            visited.Add(startBlock);

            while (toVisit.TryDequeue(out (Block Block, int Index) current))
            {
                Block block = current.Block;

                for (int i = current.Index - 1; i >= 0; i--)
                {
                    if (writes(block.OpCodes[i]))
                    {
                        return (block, i);
                    }
                }

                foreach (Block predecessor in block.Predecessors)
                {
                    if (visited.Add(predecessor))
                    {
                        toVisit.Enqueue((predecessor, predecessor.OpCodes.Count));
                    }
                }
            }

            return (null, 0);
        }

        private static bool WritesToRegister(InstOp op, int regIndex)
        {
            if (!op.Props.HasFlag(InstProps.Rd))
            {
                return false;
            }

            AmpInst inst = new(op);

            if (op.Props.HasFlag(InstProps.Tex) || op.Props.HasFlag(InstProps.TexB))
            {
                // The first two components go to the Rd pair, the others to the Rd2 pair (bits 64-71)
                int count = System.Numerics.BitOperations.PopCount((uint)inst.Bits(72, 4));

                return (regIndex >= inst.Rd && regIndex < inst.Rd + Math.Min(count, 2)) || (count > 2 && regIndex >= inst.Rc && regIndex < inst.Rc + count - 2);
            }

            return regIndex >= inst.Rd && regIndex < inst.Rd + DestRegCount(inst);
        }

        private static bool WritesToUniformRegister(InstOp op, int urIndex)
        {
            AmpInst inst = new(op);

            bool uniform = inst.Op12 is 0xab9 or 0xabb or 0x3c2 or 0x9c3 or 0x886 ||
                inst.Op9 is 0x082 or 0x087 or 0x090 or 0x091 or 0x092 or 0x096 or 0x099 or 0x09a or 0x0a4 or 0x0a5 or 0x0bd or 0x0be or 0x0bf;

            if (!uniform)
            {
                return false;
            }

            int count = (inst.Op12 is 0xab9 or 0xabb && inst.Bits(73, 3) == 5) || inst.Op9 == 0x0a5 ? 2 : 1;

            return urIndex >= inst.URd && urIndex < inst.URd + count;
        }

        // The number of consecutive registers from Rd an instruction writes
        private static int DestRegCount(AmpInst inst)
        {
            switch (inst.Op12)
            {
                case 0xb82: // LDC
                case 0x582:
                case 0x981: // LDG
                case 0x381:
                case 0x983: // LDL
                case 0x984: // LDS
                    return inst.Bits(73, 3) switch
                    {
                        5 => 2,
                        6 => 4,
                        _ => 1,
                    };
                case 0x805: // CS2R.64
                case 0x34e: // LEPC
                    return inst.Bit(80) || inst.Op12 == 0x34e ? 2 : 1;
            }

            return inst.Op9 == 0x025 ? 2 : 1;   // IMAD.WIDE
        }
    }

    // nxnative: the fields of one Ampere instruction (AmpereDecoder)
    readonly struct AmpInst
    {
        public readonly ulong Lo;
        public readonly ulong Hi;

        public AmpInst(InstOp op)
        {
            Lo = op.RawOpCode;
            Hi = op.RawOpCodeHi;
        }

        public int Bits(int start, int count)
        {
            ulong v;

            if (start >= 64)
            {
                v = Hi >> (start - 64);
            }
            else if (start + count <= 64)
            {
                v = Lo >> start;
            }
            else
            {
                v = (Lo >> start) | (Hi << (64 - start));
            }

            return (int)(v & ((1UL << count) - 1));
        }

        public bool Bit(int bit) => Bits(bit, 1) != 0;

        public int Op12 => (int)(Lo & 0xfff);
        public int Op9 => (int)(Lo & 0x1ff);
        public int Form => Bits(9, 3);

        public int GuardPred => Bits(12, 3);
        public bool GuardInv => Bit(15);

        public int Rd => Bits(16, 8);
        public int Ra => Bits(24, 8);
        public int Rb => Bits(32, 8);
        public int Rc => Bits(64, 8);
        public uint Imm32 => (uint)(Lo >> 32);

        public int URd => Bits(16, 6);
        public int URa => Bits(24, 6);
        public int URb => Bits(32, 6);
        public int URc => Bits(64, 6);

        // The constant buffer operand of forms 3 and 5 (and LDC / ULDC): byte offset in bits 38-53, bank in 54-58; bit 91 makes it bindless, the bank a UR in bits 32-37
        public int CbufOffset => Bits(38, 16);
        public int CbufSlot => Bits(54, 5);

        public int PredA => Bits(87, 3);
        public bool PredAInv => Bit(90);
        public int PredDst0 => Bits(81, 3);
        public int PredDst1 => Bits(84, 3);
    }
}
