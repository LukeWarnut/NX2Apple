using Ryujinx.Graphics.Shader.Decoders;
using Ryujinx.Graphics.Shader.IntermediateRepresentation;
using Ryujinx.Graphics.Shader.Translation;
using System;
using System.Collections.Generic;
using static Ryujinx.Graphics.Shader.Instructions.InstEmitHelper;
using static Ryujinx.Graphics.Shader.IntermediateRepresentation.OperandHelper;

namespace Ryujinx.Graphics.Shader.Instructions
{
    // nxnative: emitters for Ampere (Switch 2 NVN2) instructions, decoded by AmpereDecoder (field layouts there and in AmpInst). They build the same IR as the Maxwell
    // emitters and share their helpers where the semantics are the same (comparisons, attributes, textures, memory). Register files: R0-R254 are GPRs (R255 = RZ);
    // the uniform registers UR0-UR62 are flag registers 0-62 (UR63 = URZ, Maxwell's condition codes do not exist on Ampere) and the uniform predicates UP0-UP6 are
    // predicates 8-14 (UP7 = UPT); see RegisterConsts. Modifiers this IR has no form for (rounding modes, FTZ, FMZ) are ignored, as in the Maxwell emitters.
    static partial class InstEmit
    {
        private const int AmpUrz = 63;
        private const int AmpUPredBase = 8;

        private enum AmpSlot
        {
            B,
            C,
        }

        private static AmpInst AmpOp(EmitterContext context) => new(context.CurrOp);

        // Operand 1 and 2 of an ALU instruction, by form: 1, 4, 5, 6 take operand 1 from the B slot (bits 32-63) and operand 2 from Rc; 2, 3, 7 the other way round
        private static AmpSlot AmpSlot1(AmpInst i) => i.Form is 2 or 3 or 7 ? AmpSlot.C : AmpSlot.B;
        private static AmpSlot AmpSlot2(AmpInst i) => i.Form is 2 or 3 or 7 ? AmpSlot.B : AmpSlot.C;

        private static Operand AmpGpr(int r) => Register(r, RegisterType.Gpr);
        private static Operand AmpUr(int r) => r == AmpUrz ? Const(0) : Register(r, RegisterType.Flag);

        private static Operand AmpReg(int r, bool uniform) => uniform ? AmpUr(r) : AmpGpr(r);

        private static Operand AmpPredSrc(EmitterContext context, int p, bool inv, bool uniform = false)
        {
            Operand pred = p == RegisterConsts.PredicateTrueIndex ? Const(IrConsts.True) : Register(uniform ? AmpUPredBase + p : p, RegisterType.Predicate);

            if (inv)
            {
                pred = pred.Type == OperandType.Constant ? Const(pred.Value == IrConsts.True ? IrConsts.False : IrConsts.True) : context.BitwiseNot(pred);
            }

            return pred;
        }

        private static void AmpSetPred(EmitterContext context, int p, Operand value, bool uniform = false)
        {
            if (p != RegisterConsts.PredicateTrueIndex)
            {
                context.Copy(Register(uniform ? AmpUPredBase + p : p, RegisterType.Predicate), value);
            }
        }

        private static void AmpSetDest(EmitterContext context, int rd, Operand value, bool uniform = false)
        {
            if (uniform)
            {
                if (rd != AmpUrz)
                {
                    context.Copy(Register(rd, RegisterType.Flag), value);
                }
            }
            else if (rd != RegisterConsts.RegisterZeroIndex)
            {
                context.Copy(Register(rd, RegisterType.Gpr), value);
            }
        }

        // The constant buffer operand: bound c[bank][offset], or bindless with the bank in a UR (bit 91)
        private static Operand AmpCbuf(EmitterContext context, AmpInst i)
        {
            if (i.Bit(91))
            {
                return EmitLoadConstant(context, AmpUr(i.URb), Const(i.CbufOffset >> 2));
            }

            return Cbuf(i.CbufSlot, i.CbufOffset >> 2);
        }

        private static Operand AmpRaw(EmitterContext context, AmpInst i, AmpSlot slot, bool uniform = false)
        {
            if (slot == AmpSlot.C)
            {
                return uniform ? AmpUr(i.URc) : AmpGpr(i.Rc);
            }

            return i.Form switch
            {
                1 => uniform ? AmpUr(i.URb) : AmpGpr(i.Rb),
                2 or 4 => Const((int)i.Imm32),
                3 or 5 => AmpCbuf(context, i),
                _ => AmpUr(i.URb),
            };
        }

        private static (bool Neg, bool Abs) AmpMods(AmpInst i, AmpSlot slot)
        {
            if (slot == AmpSlot.C)
            {
                return (i.Bit(75), i.Bit(74));
            }

            // An immediate has no modifiers: bits 62 and 63 are part of it
            return i.Form is 2 or 4 ? (false, false) : (i.Bit(63), i.Bit(62));
        }

        private static Operand AmpRawA(AmpInst i, bool uniform = false) => uniform ? AmpUr(i.URa) : AmpGpr(i.Ra);

        private static Operand AmpFSrcA(EmitterContext context, AmpInst i) => context.FPAbsNeg(AmpGpr(i.Ra), i.Bit(73), i.Bit(72));

        private static Operand AmpFSrc(EmitterContext context, AmpInst i, AmpSlot slot)
        {
            (bool neg, bool abs) = AmpMods(i, slot);

            return context.FPAbsNeg(AmpRaw(context, i, slot), abs, neg);
        }

        // Integer operands: the negate bit is a two's complement negation, or a bitwise NOT for the .X (carry) forms
        private static Operand AmpISrcA(EmitterContext context, AmpInst i, bool not, bool uniform = false)
        {
            Operand src = AmpRawA(i, uniform);

            return i.Bit(72) ? (not ? context.BitwiseNot(src) : context.INegate(src)) : src;
        }

        private static Operand AmpISrc(EmitterContext context, AmpInst i, AmpSlot slot, bool not, bool uniform = false)
        {
            Operand src = AmpRaw(context, i, slot, uniform);
            (bool neg, _) = AmpMods(i, slot);

            return neg ? (not ? context.BitwiseNot(src) : context.INegate(src)) : src;
        }

        private static Operand AmpBoolToInt(EmitterContext context, Operand pred) => context.ConditionalSelect(pred, Const(1), Const(0));

        public static void AmpUnknown(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            context.TranslatorContext.GpuAccessor.Log($"Unknown Ampere instruction 0x{i.Op12:X3} at 0x{context.CurrOp.Address:X} ({i.Hi:X16}{i.Lo:X16}).");
        }

        public static void AmpNop(EmitterContext context)
        {
        }

        // ---- Floating point

        public static void AmpFadd(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            // Form 1 adds Rb; forms 2, 3 and 7 add the B slot (imm, cbuf, UR) with RZ in the Rc slot. Both are the B slot.
            Operand res = context.FPAdd(AmpFSrcA(context, i), AmpFSrc(context, i, AmpSlot.B));

            AmpSetDest(context, i.Rd, context.FPSaturate(res, i.Bit(77)));
        }

        public static void AmpFmul(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand srcA = AmpFSrcA(context, i);
            Operand srcB = AmpFSrc(context, i, AmpSlot1(i));

            // Bits 84-86: the result is scaled by 2^(4 - field); 4 is none (.D2 is 5, .M2 is 3)
            int scale = i.Bits(84, 3);

            if (scale != 4)
            {
                srcA = context.FPMultiply(srcA, ConstF(MathF.Pow(2f, 4 - scale)));
            }

            AmpSetDest(context, i.Rd, context.FPSaturate(context.FPMultiply(srcA, srcB), i.Bit(77)));
        }

        public static void AmpFfma(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand res = context.FPFusedMultiplyAdd(AmpFSrcA(context, i), AmpFSrc(context, i, AmpSlot1(i)), AmpFSrc(context, i, AmpSlot2(i)));

            AmpSetDest(context, i.Rd, context.FPSaturate(res, i.Bit(77)));
        }

        public static void AmpFmnmx(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand srcA = AmpFSrcA(context, i);
            Operand srcB = AmpFSrc(context, i, AmpSlot1(i));
            Operand min = AmpPredSrc(context, i.PredA, i.PredAInv);

            AmpSetDest(context, i.Rd, context.ConditionalSelect(min, context.FPMinimum(srcA, srcB), context.FPMaximum(srcA, srcB)));
        }

        public static void AmpFsel(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand cond = AmpPredSrc(context, i.PredA, i.PredAInv);

            AmpSetDest(context, i.Rd, context.ConditionalSelect(cond, AmpFSrcA(context, i), AmpFSrc(context, i, AmpSlot1(i))));
        }

        // FSET writes 1.0 or 0.0: every FSET in Bananza feeds FMUL, FADD or FFMA, and the SM86 repository has no other kind
        public static void AmpFset(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand res = GetFPComparison(context, (FComp)i.Bits(76, 4), AmpFSrcA(context, i), AmpFSrc(context, i, AmpSlot1(i)));

            res = InstEmitAluHelper.GetPredLogicalOp(context, (BoolOp)i.Bits(74, 2), res, AmpPredSrc(context, i.PredA, i.PredAInv));

            AmpSetDest(context, i.Rd, context.ConditionalSelect(res, ConstF(1), Const(0)));
        }

        public static void AmpFsetp(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand res = GetFPComparison(context, (FComp)i.Bits(76, 4), AmpFSrcA(context, i), AmpFSrc(context, i, AmpSlot1(i)));
            Operand pred = AmpPredSrc(context, i.PredA, i.PredAInv);
            BoolOp bop = (BoolOp)i.Bits(74, 2);

            // Both results before either is written: the destination can be the source predicate
            Operand p0 = InstEmitAluHelper.GetPredLogicalOp(context, bop, res, pred);
            Operand p1 = InstEmitAluHelper.GetPredLogicalOp(context, bop, context.BitwiseNot(res), pred);

            AmpSetPred(context, i.PredDst0, p0);
            AmpSetPred(context, i.PredDst1, p1);
        }

        // MUFU.SIN / COS take the angle in turns (Volta and later; NAK uses them for nir's fsin_normalized_2_pi), so the angle is multiplied by 2 pi
        public static void AmpMufu(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand src = AmpFSrc(context, i, AmpSlot1(i));

            Operand res = i.Bits(74, 6) switch
            {
                0 => context.FPCosine(context.FPMultiply(src, ConstF(MathF.PI * 2))),
                1 => context.FPSine(context.FPMultiply(src, ConstF(MathF.PI * 2))),
                2 => context.FPExponentB2(src),
                3 => context.FPLogarithmB2(src),
                4 => context.FPReciprocal(src),
                5 => context.FPReciprocalSquareRoot(src),
                6 => context.UnpackDouble2x32High(context.FPReciprocal(context.PackDouble2x32(Const(0), src), Instruction.FP64)),
                7 => context.UnpackDouble2x32High(context.FPReciprocalSquareRoot(context.PackDouble2x32(Const(0), src), Instruction.FP64)),
                8 => context.FPSquareRoot(src),
                _ => null,
            };

            if (res == null)
            {
                context.TranslatorContext.GpuAccessor.Log($"Unsupported Ampere MUFU operation {i.Bits(74, 6)}.");
                res = src;
            }

            AmpSetDest(context, i.Rd, res);
        }

        // Rounding: 0 nearest even, 1 down, 2 up, 3 toward zero (bits 78-79)
        private static Operand AmpRound(EmitterContext context, Operand value, int mode, Instruction fpType = Instruction.FP32)
        {
            return mode switch
            {
                0 => context.FPRound(value, fpType),
                1 => context.FPFloor(value, fpType),
                2 => context.FPCeiling(value, fpType),
                _ => context.FPTruncate(value, fpType),
            };
        }

        public static void AmpFrnd(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            bool isFP64 = i.Bits(84, 2) == 3;

            if (isFP64)
            {
                Operand src = context.PackDouble2x32(AmpGpr(i.Rb), AmpGpr(i.Rb | 1));
                Operand res = AmpRound(context, src, i.Bits(78, 2), Instruction.FP64);

                AmpSetDest(context, i.Rd, context.UnpackDouble2x32Low(res));
                AmpSetDest(context, i.Rd | 1, context.UnpackDouble2x32High(res));
                return;
            }

            AmpSetDest(context, i.Rd, AmpRound(context, AmpFSrc(context, i, AmpSlot1(i)), i.Bits(78, 2)));
        }

        // F2I: source size bits 84-85 (2 = F32, 3 = F64), destination size 75-76 (1 = 16, 2 = 32 bits), signed bit 72. The result saturates and NaN gives 0 (NVIDIA's conversion)
        public static void AmpF2i(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            bool signed = i.Bit(72);
            int dstSize = i.Bits(75, 2);

            Operand src;

            if (i.Bits(84, 2) == 3)
            {
                src = context.FP64ConvertToFP32(context.PackDouble2x32(AmpGpr(i.Rb), AmpGpr(i.Rb | 1)));
            }
            else
            {
                src = AmpFSrc(context, i, AmpSlot1(i));
            }

            Operand res = AmpRound(context, src, i.Bits(78, 2));

            // The range is clamped below the first value that does not convert (2^31 / 2^32 have no exact float neighbour below the limit), and values from the limit up give the maximum
            float min, max, limit;
            int maxInt;

            if (dstSize == 1)
            {
                (min, max, limit, maxInt) = signed ? (-32768f, 32767f, 32768f, 32767) : (0f, 65535f, 65536f, 65535);
            }
            else
            {
                (min, max, limit, maxInt) = signed ? (-2147483648f, 2147483520f, 2147483648f, int.MaxValue) : (0f, 4294967040f, 4294967296f, -1);
            }

            Operand isNan = context.IsNan(res);
            Operand overflow = context.BitwiseNot(context.FPCompareLess(res, ConstF(limit)));

            Operand clamped = context.FPMinimum(context.FPMaximum(res, ConstF(min)), ConstF(max));
            res = signed ? context.FP32ConvertToS32(clamped) : context.FP32ConvertToU32(clamped);
            res = context.ConditionalSelect(overflow, Const(maxInt), res);
            res = context.ConditionalSelect(isNan, Const(0), res);

            AmpSetDest(context, i.Rd, res);
        }

        // I2F: source size bits 84-85 (0 = 8, 1 = 16, 2 = 32 bits) with the byte or half in 60-61, signed bit 74, destination size 75-76 (1 = F16, 2 = F32)
        public static void AmpI2f(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            bool signed = i.Bit(74);
            int srcSize = i.Bits(84, 2);
            Operand src = AmpRaw(context, i, AmpSlot1(i));

            if (srcSize < 2)
            {
                int bits = 8 << srcSize;
                Operand position = Const(i.Bits(60, 2) * bits);

                src = signed ? context.BitfieldExtractS32(src, position, Const(bits)) : context.BitfieldExtractU32(src, position, Const(bits));
            }

            Operand res = signed ? context.IConvertS32ToFP32(src) : context.IConvertU32ToFP32(src);

            if (i.Bits(75, 2) == 1)
            {
                res = context.PackHalf2x16(res, ConstF(0));
            }

            AmpSetDest(context, i.Rd, res);
        }

        public static void AmpI2fp(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand src = AmpRaw(context, i, AmpSlot1(i));

            AmpSetDest(context, i.Rd, i.Bit(74) ? context.IConvertS32ToFP32(src) : context.IConvertU32ToFP32(src));
        }

        // F2F between F16 (size 1) and F32 (size 2): source size bits 84-85 with the half in 60-61, destination size 75-76
        public static void AmpF2f(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            int srcSize = i.Bits(84, 2), dstSize = i.Bits(75, 2);
            Operand src = AmpRaw(context, i, AmpSlot1(i));

            if (srcSize == 1)
            {
                src = (i.Bits(60, 2) & 1) != 0 ? context.UnpackHalf2x16High(src) : context.UnpackHalf2x16Low(src);
            }

            (bool neg, bool abs) = AmpMods(i, AmpSlot1(i));
            src = context.FPAbsNeg(src, abs, neg);

            if (srcSize == 3 || dstSize == 3)
            {
                context.TranslatorContext.GpuAccessor.Log("Unsupported Ampere F2F with a 64-bit operand.");
            }

            AmpSetDest(context, i.Rd, dstSize == 1 ? context.PackHalf2x16(src, ConstF(0)) : src);
        }

        // F2FP.PACK_AB Rd, Ra, Sb: the high half from Ra, the low half from operand 1. .MERGE_C (bit 78): the low half from operand 1, the high from Rc's half (bit 72 picks H1)
        public static void AmpF2fp(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand low = AmpRaw(context, i, AmpSlot1(i));

            if (i.Bit(78))
            {
                Operand c = AmpRaw(context, i, AmpSlot2(i));
                Operand high = i.Bit(72) ? context.ShiftRightU32(c, Const(16)) : context.BitwiseAnd(c, Const(0xffff));
                Operand packed = context.BitwiseAnd(context.PackHalf2x16(low, ConstF(0)), Const(0xffff));

                AmpSetDest(context, i.Rd, context.BitwiseOr(packed, context.ShiftLeft(high, Const(16))));
                return;
            }

            AmpSetDest(context, i.Rd, context.PackHalf2x16(low, AmpGpr(i.Ra)));
        }

        // Half precision pairs: swizzles A 74-75, B 60-61, C 81-82 (HalfSwizzle: 0 H1_H0, 1 F32, 2 H0_H0, 3 H1_H1); .F32 bit 78 writes lane 0 as a float
        private static Operand[] AmpHalfSrc(EmitterContext context, AmpInst i, int which)
        {
            Operand src;
            HalfSwizzle swizzle;
            bool neg, abs;

            if (which == 0)
            {
                (src, swizzle, neg, abs) = (AmpGpr(i.Ra), (HalfSwizzle)i.Bits(74, 2), i.Bit(72), i.Bit(73));
            }
            else
            {
                AmpSlot slot = which == 1 ? AmpSlot1(i) : AmpSlot2(i);

                src = AmpRaw(context, i, slot);

                if (slot == AmpSlot.C)
                {
                    (swizzle, neg, abs) = ((HalfSwizzle)i.Bits(81, 2), i.Bit(84), i.Bit(83));
                }
                else if (i.Form is 2 or 4)
                {
                    (swizzle, neg, abs) = (HalfSwizzle.F16, false, false);
                }
                else
                {
                    (swizzle, neg, abs) = ((HalfSwizzle)i.Bits(60, 2), i.Bit(63), i.Bit(62));
                }
            }

            return FPAbsNeg(context, GetHalfUnpacked(context, src, swizzle), abs, neg);
        }

        private static void AmpHalfResult(EmitterContext context, AmpInst i, Operand[] res)
        {
            bool sat = i.Bit(77);

            res[0] = context.FPSaturate(res[0], sat);
            res[1] = context.FPSaturate(res[1], sat);

            AmpSetDest(context, i.Rd, i.Bit(78) ? res[0] : context.PackHalf2x16(res[0], res[1]));
        }

        public static void AmpHadd2(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand[] a = AmpHalfSrc(context, i, 0), b = AmpHalfSrc(context, i, 1);

            AmpHalfResult(context, i, [context.FPAdd(a[0], b[0]), context.FPAdd(a[1], b[1])]);
        }

        public static void AmpHmul2(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand[] a = AmpHalfSrc(context, i, 0), b = AmpHalfSrc(context, i, 1);

            AmpHalfResult(context, i, [context.FPMultiply(a[0], b[0]), context.FPMultiply(a[1], b[1])]);
        }

        public static void AmpHfma2(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand[] a = AmpHalfSrc(context, i, 0), b = AmpHalfSrc(context, i, 1), c = AmpHalfSrc(context, i, 2);

            AmpHalfResult(context, i, [context.FPFusedMultiplyAdd(a[0], b[0], c[0]), context.FPFusedMultiplyAdd(a[1], b[1], c[1])]);
        }

        public static void AmpFswzadd(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            // The 2-bit operation of lane n is in bits 32 + 2n (the same as Maxwell's PnWord: 0 a+b, 1 b-a, 2 a-b, 3 b)
            AmpSetDest(context, i.Rd, context.FPSwizzleAdd(AmpGpr(i.Ra), AmpGpr(i.Rc), i.Bits(32, 8)));
        }

        // ---- Integer

        public static void AmpMov(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, i.Rd, AmpRaw(context, i, AmpSlot1(i)));
        }

        public static void AmpSel(EmitterContext context) => EmitAmpSel(context, false);

        public static void AmpUsel(EmitterContext context) => EmitAmpSel(context, true);

        private static void EmitAmpSel(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            Operand cond = AmpPredSrc(context, i.PredA, i.PredAInv, u);

            AmpSetDest(context, u ? i.URd : i.Rd, context.ConditionalSelect(cond, AmpRawA(i, u), AmpRaw(context, i, AmpSlot1(i), u)), u);
        }

        public static void AmpIabs(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, i.Rd, context.IAbsolute(AmpRaw(context, i, AmpSlot1(i))));
        }

        public static void AmpImnmx(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand srcA = AmpRawA(i), srcB = AmpRaw(context, i, AmpSlot1(i));
            bool signed = i.Bit(73);
            Operand min = signed ? context.IMinimumS32(srcA, srcB) : context.IMinimumU32(srcA, srcB);
            Operand max = signed ? context.IMaximumS32(srcA, srcB) : context.IMaximumU32(srcA, srcB);

            AmpSetDest(context, i.Rd, context.ConditionalSelect(AmpPredSrc(context, i.PredA, i.PredAInv), min, max));
        }

        public static void AmpIsetp(EmitterContext context) => EmitAmpIsetp(context, false);

        public static void AmpUisetp(EmitterContext context) => EmitAmpIsetp(context, true);

        // .EX (bit 72) compares the high words of a 64-bit pair, with the low words' result in the predicate at 68-70 (71 inverts it)
        private static void EmitAmpIsetp(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            IComp cmp = (IComp)i.Bits(76, 3);
            bool signed = i.Bit(73);
            Operand srcA = AmpRawA(i, u), srcB = AmpRaw(context, i, AmpSlot1(i), u);
            Operand res;

            if (i.Bit(72))
            {
                Operand low = AmpPredSrc(context, i.Bits(68, 3), i.Bit(71), u);

                res = cmp switch
                {
                    IComp.Eq => context.BitwiseAnd(context.ICompareEqual(srcA, srcB), low),
                    IComp.Ne => context.BitwiseOr(context.ICompareNotEqual(srcA, srcB), low),
                    IComp.Lt or IComp.Le => context.BitwiseOr(GetIntComparison(context, IComp.Lt, srcA, srcB, signed), context.BitwiseAnd(context.ICompareEqual(srcA, srcB), low)),
                    IComp.Gt or IComp.Ge => context.BitwiseOr(GetIntComparison(context, IComp.Gt, srcA, srcB, signed), context.BitwiseAnd(context.ICompareEqual(srcA, srcB), low)),
                    _ => GetIntComparison(context, cmp, srcA, srcB, signed),
                };
            }
            else
            {
                res = GetIntComparison(context, cmp, srcA, srcB, signed);
            }

            Operand pred = AmpPredSrc(context, i.PredA, i.PredAInv, u);
            BoolOp bop = (BoolOp)i.Bits(74, 2);

            Operand p0 = InstEmitAluHelper.GetPredLogicalOp(context, bop, res, pred);
            Operand p1 = InstEmitAluHelper.GetPredLogicalOp(context, bop, context.BitwiseNot(res), pred);

            AmpSetPred(context, i.PredDst0, p0, u);
            AmpSetPred(context, i.PredDst1, p1, u);
        }

        public static void AmpIadd3(EmitterContext context) => EmitAmpIadd3(context, false);

        public static void AmpUiadd3(EmitterContext context) => EmitAmpIadd3(context, true);

        // IADD3 Rd, P0, P1, Ra, Sb, Rc: the carries of the two additions go to P0 and P1. .X (bit 74) adds the carry predicates at 87-90 and 77-80, and its negations are bitwise NOTs.
        private static void EmitAmpIadd3(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            bool x = i.Bit(74);
            Operand a = AmpISrcA(context, i, x, u);
            Operand b = AmpISrc(context, i, AmpSlot1(i), x, u);
            Operand c = AmpISrc(context, i, AmpSlot2(i), x, u);

            Operand ab = context.IAdd(a, b);
            Operand res = context.IAdd(ab, c);

            if (x)
            {
                res = context.IAdd(res, AmpBoolToInt(context, AmpPredSrc(context, i.PredA, i.PredAInv, u)));
                res = context.IAdd(res, AmpBoolToInt(context, AmpPredSrc(context, i.Bits(77, 3), i.Bit(80), u)));
            }

            if (i.PredDst0 != RegisterConsts.PredicateTrueIndex || i.PredDst1 != RegisterConsts.PredicateTrueIndex)
            {
                // The carry out of a + b + c (+ carries) is 0, 1 or 2: P0 has the first, P1 the second. Computed in 64 bits from the three (or five) 32-bit terms.
                Operand carry1 = context.ICompareLessUnsigned(ab, a);
                Operand carry2 = context.ICompareLessUnsigned(res, ab);

                AmpSetPred(context, i.PredDst0, carry1, u);
                AmpSetPred(context, i.PredDst1, carry2, u);
            }

            AmpSetDest(context, u ? i.URd : i.Rd, res, u);
        }

        public static void AmpImad(EmitterContext context) => EmitAmpImad(context, false, false);

        public static void AmpUimad(EmitterContext context) => EmitAmpImad(context, true, false);

        public static void AmpImadWide(EmitterContext context) => EmitAmpImad(context, false, true);

        public static void AmpUimadWide(EmitterContext context) => EmitAmpImad(context, true, true);

        // IMAD Rd, Ra, Sb, Rc: Ra * Sb + Rc; .WIDE adds the 64-bit Rc pair and writes the Rd pair; signed bit 73; .X (bit 74) adds the carry predicate at 87-90
        private static void EmitAmpImad(EmitterContext context, bool u, bool wide)
        {
            AmpInst i = AmpOp(context);

            bool signed = i.Bit(73);
            Operand a = AmpRawA(i, u);
            Operand b = AmpRaw(context, i, AmpSlot1(i), u);
            Operand c = AmpISrc(context, i, AmpSlot2(i), i.Bit(74), u);
            int rd = u ? i.URd : i.Rd;

            Operand low = context.IMultiply(a, b);

            if (!wide)
            {
                Operand res = context.IAdd(low, c);

                if (i.Bit(74))
                {
                    res = context.IAdd(res, AmpBoolToInt(context, AmpPredSrc(context, i.PredA, i.PredAInv, u)));
                }

                AmpSetDest(context, rd, res, u);
                return;
            }

            Operand high = signed ? context.MultiplyHighS32(a, b) : context.MultiplyHighU32(a, b);
            Operand cHigh = AmpSlot2(i) == AmpSlot.C ? (u ? AmpUr(i.URc == AmpUrz ? AmpUrz : i.URc + 1) : AmpGpr(i.Rc == RegisterConsts.RegisterZeroIndex ? i.Rc : i.Rc + 1)) : Const(0);

            Operand resLow = context.IAdd(low, c);
            Operand carry = context.ICompareLessUnsigned(resLow, low);
            Operand resHigh = context.IAdd(context.IAdd(high, cHigh), AmpBoolToInt(context, carry));

            AmpSetDest(context, rd, resLow, u);
            AmpSetDest(context, u ? (rd == AmpUrz ? rd : rd + 1) : (rd == RegisterConsts.RegisterZeroIndex ? rd : rd + 1), resHigh, u);
        }

        public static void AmpImadHi(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand a = AmpRawA(i), b = AmpRaw(context, i, AmpSlot1(i));
            Operand high = i.Bit(73) ? context.MultiplyHighS32(a, b) : context.MultiplyHighU32(a, b);

            AmpSetDest(context, i.Rd, context.IAdd(high, AmpISrc(context, i, AmpSlot2(i), false)));
        }

        public static void AmpLea(EmitterContext context) => EmitAmpLea(context, false);

        public static void AmpUlea(EmitterContext context) => EmitAmpLea(context, true);

        // LEA Rd, P, Ra, Sb, Rc, shift: (Ra << shift) + Sb. .HI (bit 80): the high word of (Rc:Ra) << shift, plus Sb (.SX32, bit 73: Rc is Ra's sign). .X (bit 74) adds the carry
        // predicate at 87-90; P (81-83) is the carry out. Bit 72 negates Ra (NOT for .X).
        private static void EmitAmpLea(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            bool x = i.Bit(74);
            int shift = i.Bits(75, 5);
            Operand a = AmpISrcA(context, i, x, u);
            Operand b = AmpRaw(context, i, AmpSlot1(i), u);
            Operand shifted;

            if (i.Bit(80))
            {
                Operand c = i.Bit(73) ? context.ShiftRightS32(a, Const(31)) : AmpRaw(context, i, AmpSlot2(i), u);

                shifted = shift == 0 ? c : context.BitwiseOr(context.ShiftLeft(c, Const(shift)), context.ShiftRightU32(a, Const(32 - shift)));
            }
            else
            {
                shifted = context.ShiftLeft(a, Const(shift));
            }

            Operand res = context.IAdd(shifted, b);

            if (x)
            {
                res = context.IAdd(res, AmpBoolToInt(context, AmpPredSrc(context, i.PredA, i.PredAInv, u)));
            }

            AmpSetPred(context, i.PredDst0, context.ICompareLessUnsigned(res, shifted), u);
            AmpSetDest(context, u ? i.URd : i.Rd, res, u);
        }

        public static void AmpLop3(EmitterContext context) => EmitAmpLop3(context, false);

        public static void AmpUlop3(EmitterContext context) => EmitAmpLop3(context, true);

        // LOP3.LUT P, Rd, Ra, Sb, Rc, lut, Pp: P = (Rd != 0) OR Pp (AND with .PAND, bit 80)
        private static void EmitAmpLop3(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            Operand res = Lop3Expression.GetFromTruthTable(context, AmpRawA(i, u), AmpRaw(context, i, AmpSlot1(i), u), AmpRaw(context, i, AmpSlot2(i), u), i.Bits(72, 8));

            if (i.PredDst0 != RegisterConsts.PredicateTrueIndex)
            {
                Operand nonZero = context.ICompareNotEqual(res, Const(0));
                Operand pred = AmpPredSrc(context, i.PredA, i.PredAInv, u);

                AmpSetPred(context, i.PredDst0, i.Bit(80) ? context.BitwiseAnd(nonZero, pred) : context.BitwiseOr(nonZero, pred), u);
            }

            AmpSetDest(context, u ? i.URd : i.Rd, res, u);
        }

        public static void AmpShf(EmitterContext context) => EmitAmpShf(context, false);

        public static void AmpUshf(EmitterContext context) => EmitAmpShf(context, true);

        // SHF.{L,R}.{S64,U64,S32,U32}{.W}{.HI} Rd, Ra, Sb, Rc: a funnel shift of the 64-bit Rc:Ra by Sb, writing the low word (or the high with .HI). .W wraps the shift at the type's
        // width, otherwise it is clamped to it. The 32-bit types fill a right shift of the high word with zeros or its sign.
        private static void EmitAmpShf(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            int type = i.Bits(73, 2);
            bool is64 = type < 2, signed = (type & 1) == 0, wrap = i.Bit(75), right = i.Bit(76), hi = i.Bit(80);
            Operand low = AmpRawA(i, u), shift = AmpRaw(context, i, AmpSlot1(i), u), high = AmpRaw(context, i, AmpSlot2(i), u);
            int width = is64 ? 64 : 32;

            shift = wrap ? context.BitwiseAnd(shift, Const(width - 1)) : context.IMinimumU32(shift, Const(width));

            // (Rc:Ra) shifted by s, for s in [0, 64]; shifts by 32 or more are done on the other word (SPIR-V shifts are undefined at 32)
            Operand ge32 = context.ICompareGreaterOrEqualUnsigned(shift, Const(32));
            Operand s = context.BitwiseAnd(shift, Const(31));
            Operand s0 = context.ICompareEqual(s, Const(0));
            Operand inv = context.ISubtract(Const(32), s);
            Operand fill = signed ? context.ShiftRightS32(high, Const(31)) : Const(0);
            Operand res;

            if (right)
            {
                // Low word: (low >> s) | (high << (32 - s)); s >= 32: high >> (s - 32). High word: high >> s; s >= 32: fill.
                Operand highShr = signed ? context.ShiftRightS32(high, s) : context.ShiftRightU32(high, s);

                if (hi)
                {
                    res = context.ConditionalSelect(ge32, fill, highShr);
                }
                else
                {
                    Operand lowWord = context.BitwiseOr(context.ShiftRightU32(low, s), context.ConditionalSelect(s0, Const(0), context.ShiftLeft(high, inv)));

                    res = context.ConditionalSelect(ge32, context.ConditionalSelect(context.ICompareEqual(shift, Const(64)), fill, highShr), lowWord);
                }
            }
            else
            {
                // High word: (high << s) | (low >> (32 - s)); s >= 32: low << (s - 32). Low word: low << s; s >= 32: 0.
                Operand lowShl = context.ShiftLeft(low, s);

                if (hi)
                {
                    Operand highWord = context.BitwiseOr(context.ShiftLeft(high, s), context.ConditionalSelect(s0, Const(0), context.ShiftRightU32(low, inv)));

                    res = context.ConditionalSelect(ge32, context.ConditionalSelect(context.ICompareEqual(shift, Const(64)), Const(0), lowShl), highWord);
                }
                else
                {
                    res = context.ConditionalSelect(ge32, Const(0), lowShl);
                }
            }

            AmpSetDest(context, u ? i.URd : i.Rd, res, u);
        }

        public static void AmpPrmt(EmitterContext context) => EmitAmpPrmt(context, false);

        public static void AmpUprmt(EmitterContext context) => EmitAmpPrmt(context, true);

        // PRMT Rd, Ra, Sb, Rc: bytes of Rc:Ra chosen by Sb (PTX prmt; mode bits 72-74: 0 index, 1 f4e, 2 b4e, 3 rc8, 4 ecl, 5 ecr, 6 rc16)
        private static void EmitAmpPrmt(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            Operand a = AmpRawA(i, u), sel = AmpRaw(context, i, AmpSlot1(i), u), c = AmpRaw(context, i, AmpSlot2(i), u);
            int mode = i.Bits(72, 3);

            Operand Byte(Operand index)
            {
                // index 0-3 from Ra, 4-7 from Rc
                Operand word = context.ConditionalSelect(context.ICompareGreaterOrEqualUnsigned(index, Const(4)), c, a);
                return context.BitfieldExtractU32(word, context.ShiftLeft(context.BitwiseAnd(index, Const(3)), Const(3)), Const(8));
            }

            Operand res = Const(0);

            if (mode == 0)
            {
                for (int b = 0; b < 4; b++)
                {
                    Operand nibble = context.BitfieldExtractU32(sel, Const(b * 4), Const(4));
                    Operand value = Byte(context.BitwiseAnd(nibble, Const(7)));
                    Operand sign = context.ShiftRightS32(context.ShiftLeft(value, Const(24)), Const(31));

                    value = context.ConditionalSelect(context.ICompareNotEqual(context.BitwiseAnd(nibble, Const(8)), Const(0)), context.BitwiseAnd(sign, Const(0xff)), value);
                    res = context.BitwiseOr(res, context.ShiftLeft(value, Const(b * 8)));
                }
            }
            else
            {
                Operand s = context.BitwiseAnd(sel, Const(3));

                for (int b = 0; b < 4; b++)
                {
                    Operand index = mode switch
                    {
                        1 => context.BitwiseAnd(context.IAdd(s, Const(b)), Const(7)),
                        2 => context.BitwiseAnd(context.ISubtract(s, Const(b)), Const(7)),
                        3 => s,
                        4 => context.IMinimumU32(Const(b), s),
                        5 => context.IMaximumU32(Const(b), s),
                        _ => context.BitwiseOr(context.ShiftLeft(context.BitwiseAnd(s, Const(1)), Const(1)), Const(b & 1)),
                    };

                    res = context.BitwiseOr(res, context.ShiftLeft(Byte(index), Const(b * 8)));
                }
            }

            AmpSetDest(context, u ? i.URd : i.Rd, res, u);
        }

        public static void AmpSgxt(EmitterContext context) => EmitAmpSgxt(context, false);

        public static void AmpUsgxt(EmitterContext context) => EmitAmpSgxt(context, true);

        // SGXT Rd, Ra, Sb: the low Sb bits of Ra, sign (bit 73) or zero extended
        private static void EmitAmpSgxt(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            Operand a = AmpRawA(i, u);
            Operand bits = context.IMinimumU32(AmpRaw(context, i, AmpSlot1(i), u), Const(32));
            Operand res = i.Bit(73) ? context.BitfieldExtractS32(a, Const(0), bits) : context.BitfieldExtractU32(a, Const(0), bits);

            AmpSetDest(context, u ? i.URd : i.Rd, context.ConditionalSelect(context.ICompareEqual(bits, Const(0)), Const(0), res), u);
        }

        // BMSK Rd, Ra, Sb: Sb one bits from bit Ra
        public static void AmpBmsk(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand pos = AmpRawA(i), width = AmpRaw(context, i, AmpSlot1(i));

            if (i.Bit(75))
            {
                pos = context.BitwiseAnd(pos, Const(31));
                width = context.BitwiseAnd(width, Const(31));
            }

            Operand ones = context.ConditionalSelect(context.ICompareGreaterOrEqualUnsigned(width, Const(32)), Const(-1), context.ISubtract(context.ShiftLeft(Const(1), width), Const(1)));

            AmpSetDest(context, i.Rd, context.ConditionalSelect(context.ICompareGreaterOrEqualUnsigned(pos, Const(32)), Const(0), context.ShiftLeft(ones, pos)));
        }

        public static void AmpPopc(EmitterContext context) => EmitAmpPopc(context, false);

        public static void AmpUpopc(EmitterContext context) => EmitAmpPopc(context, true);

        private static void EmitAmpPopc(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            Operand src = AmpRaw(context, i, AmpSlot1(i), u);

            AmpSetDest(context, u ? i.URd : i.Rd, context.BitCount(i.Bit(63) ? context.BitwiseNot(src) : src), u);
        }

        public static void AmpFlo(EmitterContext context) => EmitAmpFlo(context, false);

        public static void AmpUflo(EmitterContext context) => EmitAmpFlo(context, true);

        // FLO Rd, Sb: the position of the highest one (of the highest bit unlike the sign, signed bit 73), -1 when there is none; .SH (bit 74) gives 31 minus it
        private static void EmitAmpFlo(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            Operand src = AmpRaw(context, i, AmpSlot1(i), u);

            if (i.Bit(63))
            {
                src = context.BitwiseNot(src);
            }

            Operand res = i.Bit(73) ? context.FindMSBS32(src) : context.FindMSBU32(src);

            if (i.Bit(74))
            {
                res = context.ConditionalSelect(context.ICompareEqual(res, Const(-1)), Const(-1), context.ISubtract(Const(31), res));
            }

            AmpSetDest(context, u ? i.URd : i.Rd, res, u);
        }

        public static void AmpBrev(EmitterContext context) => EmitAmpBrev(context, false);

        public static void AmpUbrev(EmitterContext context) => EmitAmpBrev(context, true);

        private static void EmitAmpBrev(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, u ? i.URd : i.Rd, context.BitfieldReverse(AmpRaw(context, i, AmpSlot1(i), u)), u);
        }

        // ---- Predicates and special registers

        // P2R Rd, PR, Ra, mask: P0-P6 placed in byte 76-77 of Rd under mask (shifted with them), the other bits from Ra
        public static void AmpP2r(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            int shift = i.Bits(76, 2) * 8;
            Operand mask = context.ShiftLeft(AmpRaw(context, i, AmpSlot1(i)), Const(shift));
            Operand preds = Const(0);

            for (int p = 0; p < RegisterConsts.PredsCount; p++)
            {
                preds = context.BitwiseOr(preds, context.ConditionalSelect(AmpPredSrc(context, p, false), Const(1 << (p + shift)), Const(0)));
            }

            AmpSetDest(context, i.Rd, context.BitwiseOr(context.BitwiseAnd(AmpRawA(i), context.BitwiseNot(mask)), context.BitwiseAnd(preds, mask)));
        }

        // R2P PR, Ra, mask: Pn = bit n of byte 76-77 of Ra, for the bits set in mask
        public static void AmpR2p(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            int shift = i.Bits(76, 2) * 8;
            Operand value = AmpRawA(i);
            Operand mask = AmpRaw(context, i, AmpSlot1(i));

            for (int p = 0; p < RegisterConsts.PredsCount; p++)
            {
                Operand bit = Const(1 << (p + shift));
                Operand isSet = context.ICompareNotEqual(context.BitwiseAnd(value, bit), Const(0));
                Operand masked = context.ICompareNotEqual(context.BitwiseAnd(mask, Const(1 << p)), Const(0));
                Operand pred = Register(p, RegisterType.Predicate);

                context.Copy(pred, context.ConditionalSelect(masked, isSet, pred));
            }
        }

        // PLOP3.LUT Pu, Pv, Pa, Pb, Pc, lut0, lut1: Pa at 87-90, Pb at 77-80, Pc at 68-71 (a uniform predicate with bit 67); lut0 is bits 64-66 and 72-76, lut1 bits 16-23
        public static void AmpPlop3(EmitterContext context) => EmitAmpPlop3(context, false);

        public static void AmpUplop3(EmitterContext context) => EmitAmpPlop3(context, true);

        private static void EmitAmpPlop3(EmitterContext context, bool u)
        {
            AmpInst i = AmpOp(context);

            Operand a = AmpBoolToMask(context, AmpPredSrc(context, i.PredA, i.PredAInv, u));
            Operand b = AmpBoolToMask(context, AmpPredSrc(context, i.Bits(77, 3), i.Bit(80), u));
            Operand c = AmpBoolToMask(context, AmpPredSrc(context, i.Bits(68, 3), i.Bit(71), u || i.Bit(67)));

            int lut0 = i.Bits(64, 3) | (i.Bits(72, 5) << 3);
            int lut1 = i.Bits(16, 8);

            AmpSetPred(context, i.PredDst0, context.ICompareNotEqual(Lop3Expression.GetFromTruthTable(context, a, b, c, lut0), Const(0)), u);
            AmpSetPred(context, i.PredDst1, context.ICompareNotEqual(Lop3Expression.GetFromTruthTable(context, a, b, c, lut1), Const(0)), u);
        }

        private static Operand AmpBoolToMask(EmitterContext context, Operand pred) => context.ConditionalSelect(pred, Const(-1), Const(0));

        public static void AmpS2r(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, i.Rd, EmitS2r(context, (SReg)i.Bits(72, 8)));
        }

        public static void AmpS2ur(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, i.URd, EmitS2r(context, (SReg)i.Bits(72, 8)), true);
        }

        // CS2R Rd, SR: SRZ (0xff) and the clocks read as 0; bit 80 writes the Rd pair
        public static void AmpCs2r(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            int sr = i.Bits(72, 8);

            if (sr != 0xff)
            {
                context.TranslatorContext.GpuAccessor.Log($"Ampere CS2R of special register 0x{sr:X} reads 0.");
            }

            AmpSetDest(context, i.Rd, Const(0));

            if (i.Bit(80) && i.Rd != RegisterConsts.RegisterZeroIndex)
            {
                AmpSetDest(context, i.Rd + 1, Const(0));
            }
        }

        public static void AmpR2ur(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, i.URd, AmpGpr(i.Ra), true);
        }

        public static void AmpUmov(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, i.URd, i.Form is 2 or 4 ? Const((int)i.Imm32) : AmpUr(i.URb), true);
        }

        // LEPC Rd: the address of this instruction (the geometry shaders' prologue)
        public static void AmpLepc(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, i.Rd, Const((int)context.CurrOp.Address));
            AmpSetDest(context, i.Rd == RegisterConsts.RegisterZeroIndex ? i.Rd : i.Rd + 1, Const(0));
        }

        public static void AmpVote(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand pred = AmpPredSrc(context, i.PredA, i.PredAInv);
            Operand res = EmitVote(context, (VoteMode)i.Bits(72, 2), pred);
            Operand ballot = i.Rd != RegisterConsts.RegisterZeroIndex ? EmitBallot(context, pred) : null;

            if (res != null)
            {
                AmpSetPred(context, i.PredDst0, res);
            }

            if (ballot != null)
            {
                AmpSetDest(context, i.Rd, ballot);
            }
        }

        public static void AmpVoteu(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand pred = AmpPredSrc(context, i.PredA, i.PredAInv);
            Operand res = EmitVote(context, (VoteMode)i.Bits(72, 2), pred);
            Operand ballot = i.URd != AmpUrz ? EmitBallot(context, pred) : null;

            if (res != null)
            {
                AmpSetPred(context, i.PredDst0, res, true);
            }

            if (ballot != null)
            {
                AmpSetDest(context, i.URd, ballot, true);
            }
        }

        // SHFL.mode Pd, Rd, Ra, lane, c: lane in Rb (0x389, 0x589) or bits 53-57; c (clamp and segment mask, as Maxwell's) in Rc (0x389, 0x989) or bits 40-52; mode bits 58-59
        public static void AmpShfl(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand srcA = AmpGpr(i.Ra);
            Operand lane = (i.Op12 is 0x389 or 0x589) ? AmpGpr(i.Rb) : Const(i.Bits(53, 5));
            Operand c = (i.Op12 is 0x389 or 0x989) ? AmpGpr(i.Rc) : Const(i.Bits(40, 13));

            (Operand res, Operand valid) = (ShflMode)i.Bits(58, 2) switch
            {
                ShflMode.Idx => context.Shuffle(srcA, lane, c),
                ShflMode.Up => context.ShuffleUp(srcA, lane, c),
                ShflMode.Down => context.ShuffleDown(srcA, lane, c),
                _ => context.ShuffleXor(srcA, lane, c),
            };

            AmpSetDest(context, i.Rd, res);
            AmpSetPred(context, i.PredDst0, valid);
        }

        // ---- Constants and memory

        // ULDC URd, c[bank][offset]; 0xabb adds a UR at 24-29 to the offset; 0xab9 with bit 91 is bindless with the bank in that UR. Size bits 73-75 (LsSize).
        public static void AmpUldc(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            LsSize2 size = (LsSize2)i.Bits(73, 3);
            int offset = i.CbufOffset;
            int count = size == LsSize2.B64 ? 2 : size == LsSize2.B128 ? 4 : 1;

            if (i.Op12 == 0xab9 && !i.Bit(91) && (offset & 3) == 0 && size >= LsSize2.B32)
            {
                // A constant offset stays a constant buffer operand: GlobalToStorage finds storage buffer addresses by it
                for (int index = 0; index < count; index++)
                {
                    AmpSetDest(context, i.URd + index >= AmpUrz ? AmpUrz : i.URd + index, Cbuf(i.CbufSlot, (offset >> 2) + index), true);
                }

                return;
            }

            Operand slot = i.Op12 == 0xab9 && i.Bit(91) ? AmpUr(i.URa) : Const(i.CbufSlot);
            Operand addr = i.Op12 == 0xabb ? context.IAdd(AmpUr(i.URa), Const(offset)) : Const(offset);

            EmitAmpLoadConstant(context, slot, addr, size, count, (index, value) => AmpSetDest(context, i.URd + index >= AmpUrz ? AmpUrz : i.URd + index, value, true));
        }

        // LDC Rd, c[bank][Ra + offset]: mode bits 78-79 (2 and 3: the bank is Ra's high half, as Maxwell's .IS); 0x582 is bindless, the bank a UR at 24-29 and the index in Rc
        public static void AmpLdc(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            LsSize2 size = (LsSize2)i.Bits(73, 3);
            int count = size == LsSize2.B64 ? 2 : size == LsSize2.B128 ? 4 : 1;
            Operand slot, srcA;

            if (i.Op12 == 0x582)
            {
                slot = AmpUr(i.URa);
                srcA = AmpGpr(i.Rc);
            }
            else
            {
                slot = Const(i.CbufSlot);
                srcA = AmpGpr(i.Ra);

                if (i.Bits(78, 2) >= 2)
                {
                    slot = context.IAdd(slot, context.BitfieldExtractU32(srcA, Const(16), Const(16)));
                    srcA = context.BitwiseAnd(srcA, Const(0xffff));
                }
            }

            Operand addr = context.IAdd(srcA, Const(i.CbufOffset));

            EmitAmpLoadConstant(context, slot, addr, size, count, (index, value) => AmpSetDest(context, i.Rd + index >= RegisterConsts.RegisterZeroIndex ? RegisterConsts.RegisterZeroIndex : i.Rd + index, value));
        }

        private static void EmitAmpLoadConstant(EmitterContext context, Operand slot, Operand addr, LsSize2 size, int count, Action<int, Operand> setDest)
        {
            Operand wordOffset = context.ShiftRightU32(addr, Const(2));

            for (int index = 0; index < count; index++)
            {
                Operand value = EmitLoadConstant(context, slot, context.IAdd(wordOffset, Const(index)));

                if (size < LsSize2.B32)
                {
                    value = ExtractSmallInt(context, (LsSize)size, GetBitOffset(context, addr), value);
                }

                setDest(index, value);
            }
        }

        // The global address of LDG / STG / ATOMG / RED: Ra (the Ra pair with the 64-bit bit), plus a UR (or pair) when bit 91 is set, plus a signed 24-bit offset (40-63).
        // AmpGlobalBase reads the registers once (copies, so a destination that is also the address cannot change it); AmpGlobalOffset adds the instruction's offset and the
        // component's in one constant: GlobalToStorage finds the storage buffer through one constant offset over (Ra + the c[0] address a ULDC loaded), as for Maxwell's LDG.
        private static (Operand Low, Operand High) AmpGlobalBase(EmitterContext context, AmpInst i, int ra64Bit, int urStart, int ur64Bit)
        {
            bool ra64 = i.Bit(ra64Bit) && i.Ra != RegisterConsts.RegisterZeroIndex;
            Operand low = AmpGpr(i.Ra);
            Operand high = ra64 ? AmpGpr(i.Ra + 1) : Const(0);

            if (i.Bit(91) && i.Bits(urStart, 6) != AmpUrz)
            {
                int ur = i.Bits(urStart, 6);
                Operand urLow = AmpUr(ur);
                Operand urHigh = i.Bit(ur64Bit) ? AmpUr(ur + 1) : Const(0);

                Operand sum = context.IAdd(low, urLow);
                Operand carry = context.ICompareLessUnsigned(sum, urLow);

                high = context.IAdd(context.IAdd(high, urHigh), AmpBoolToInt(context, carry));
                low = sum;
            }

            return (context.Copy(low), context.Copy(high));
        }

        private static (Operand Low, Operand High) AmpGlobalOffset(EmitterContext context, AmpInst i, (Operand Low, Operand High) addr, int extraOffset)
        {
            int offset = (i.Bits(40, 24) << 8 >> 8) + extraOffset;

            if (offset == 0)
            {
                return addr;
            }

            Operand offs = Const(offset);
            Operand low = context.IAdd(addr.Low, offs);
            Operand carry = offset > 0 ? context.ICompareLessUnsigned(low, offs) : context.ICompareLessUnsigned(addr.Low, Const(-offset));
            Operand high = offset > 0 ? context.IAdd(addr.High, AmpBoolToInt(context, carry)) : context.ISubtract(addr.High, AmpBoolToInt(context, carry));

            return (low, high);
        }

        private static (Operand Low, Operand High) AmpGlobalAddress(EmitterContext context, AmpInst i, int ra64Bit, int urStart, int ur64Bit)
        {
            return AmpGlobalOffset(context, i, AmpGlobalBase(context, i, ra64Bit, urStart, ur64Bit), 0);
        }

        private static StorageKind AmpGlobalKind(LsSize2 size) => GetStorageKind((LsSize)size);

        private static int AmpVectorCount(LsSize2 size) => size == LsSize2.B64 ? 2 : size == LsSize2.B128 ? 4 : 1;

        public static void AmpLdg(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            LsSize2 size = (LsSize2)i.Bits(73, 3);
            StorageKind kind = AmpGlobalKind(size);

            // The registers are read once, before the destinations are written: LDG.128 R0, [R0] is common
            (Operand Low, Operand High) addrBase = AmpGlobalBase(context, i, 90, 32, 72);

            for (int index = 0; index < AmpVectorCount(size); index++)
            {
                if (i.Rd + index >= RegisterConsts.RegisterZeroIndex)
                {
                    break;
                }

                (Operand low, Operand high) = AmpGlobalOffset(context, i, addrBase, index * 4);

                AmpSetDest(context, i.Rd + index, context.Load(kind, low, high));
            }
        }

        public static void AmpStg(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            LsSize2 size = (LsSize2)i.Bits(73, 3);
            StorageKind kind = AmpGlobalKind(size);

            (Operand Low, Operand High) addrBase = AmpGlobalBase(context, i, 90, 64, 72);

            for (int index = 0; index < AmpVectorCount(size); index++)
            {
                bool isRz = i.Rb + index >= RegisterConsts.RegisterZeroIndex;

                (Operand low, Operand high) = AmpGlobalOffset(context, i, addrBase, index * 4);

                context.Store(kind, low, high, AmpGpr(isRz ? i.Rb : i.Rb + index));
            }
        }

        // ATOMG.op Rd, [Ra + UR + offset], Rb: operation bits 87-90, type 73-75 (AtomSize); Ra is 64-bit with bit 70 here, the UR at 64-69
        public static void AmpAtomg(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            (Operand low, Operand high) = AmpGlobalAddress(context, i, i.Op12 == 0x9a8 ? 70 : 72, 64, 72);

            Operand res = EmitAtomicOp(context, StorageKind.GlobalMemory, (AtomOp)i.Bits(87, 4), (AtomSize)i.Bits(73, 3), low, high, AmpGpr(i.Rb));

            AmpSetDest(context, i.Rd, res);
        }

        // ATOMG.CAS Rd, [Ra + offset], Rb (compare), Rc (value)
        public static void AmpAtomgCas(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            (Operand low, Operand high) = AmpGlobalAddress(context, i, 72, 64, 72);

            AmpSetDest(context, i.Rd, context.AtomicCompareAndSwap(StorageKind.GlobalMemory, low, high, AmpGpr(i.Rb), AmpGpr(i.Rc)));
        }

        // RED.op [Ra + UR + offset], Rb: operation bits 87-89, type 73-75
        public static void AmpRed(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            (Operand low, Operand high) = AmpGlobalAddress(context, i, 90, 64, 72);

            EmitAtomicOp(context, StorageKind.GlobalMemory, (AtomOp)i.Bits(87, 3), (AtomSize)i.Bits(73, 3), low, high, AmpGpr(i.Rb));
        }

        // Local and shared memory: Ra + UR (bit 91 is always set by NVIDIA's compiler, the UR URZ when there is none) + offset
        private static Operand AmpLocalAddress(EmitterContext context, AmpInst i, int urStart)
        {
            Operand addr = AmpGpr(i.Ra);

            if (i.Bit(91) && i.Bits(urStart, 6) != AmpUrz)
            {
                addr = context.IAdd(addr, AmpUr(i.Bits(urStart, 6)));
            }

            return addr;
        }

        public static void AmpLdl(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            EmitLoad(context, StorageKind.LocalMemory, (LsSize2)i.Bits(73, 3), AmpLocalAddress(context, i, 32), i.Rd, i.Bits(40, 24) << 8 >> 8);
        }

        public static void AmpStl(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            EmitStore(context, StorageKind.LocalMemory, (LsSize2)i.Bits(73, 3), AmpLocalAddress(context, i, 64), i.Rb, i.Bits(40, 24) << 8 >> 8);
        }

        public static void AmpLds(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            EmitLoad(context, StorageKind.SharedMemory, (LsSize2)i.Bits(73, 3), AmpLocalAddress(context, i, 32), i.Rd, i.Bits(40, 24) << 8 >> 8);
        }

        public static void AmpSts(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            EmitStore(context, StorageKind.SharedMemory, (LsSize2)i.Bits(73, 3), AmpLocalAddress(context, i, 64), i.Rb, i.Bits(40, 24) << 8 >> 8);
        }

        // ATOMS.op Rd, [Ra + offset], Rb: operation bits 87-90, type 73-75
        public static void AmpAtoms(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            Operand offset = context.ShiftRightU32(context.IAdd(AmpLocalAddress(context, i, 64), Const(i.Bits(40, 24) << 8 >> 8)), Const(2));
            Operand id = Const(context.ResourceManager.SharedMemoryId);

            AmpSetDest(context, i.Rd, EmitAtomicOp(context, StorageKind.SharedMemory, (AtomOp)i.Bits(87, 4), (AtomSize)i.Bits(73, 3), id, offset, AmpGpr(i.Rb)));
        }

        public static void AmpBar(EmitterContext context)
        {
            context.Barrier();
        }

        public static void AmpMembar(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            if (i.Bits(76, 3) == 0)
            {
                context.GroupMemoryBarrier();
            }
            else
            {
                context.MemoryBarrier();
            }
        }

        // ---- Attributes

        // ALD.size Rd, a[Ra + addr], Rb: address bits 40-49, components - 1 in 74-75, .P 76, .PHYS 77, .O 79; Rb is the vertex
        public static void AmpAld(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            EmitAld(context, i.Rd, i.Ra, i.Rb, i.Bits(40, 10), i.Bits(74, 2) + 1, i.Bit(79), i.Bit(76), i.Bit(77));
        }

        // AST.size a[Ra + addr], Rb (data), Rc (vertex)
        public static void AmpAst(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            EmitAst(context, i.Ra, i.Rb, i.Bits(40, 10), i.Bits(74, 2) + 1, i.Bit(76), i.Bit(77));
        }

        // IPA Rd, a[addr]: the address / 4 in bits 64-71; interpolation location 76-77 and frequency 78-79 come from the SPH's input map, as for Maxwell
        public static void AmpIpa(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            EmitIpa(context, i.Rd, RegisterConsts.RegisterZeroIndex, i.Bits(64, 8) * 4, false, -1, false);
        }

        public static void AmpAl2p(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, i.Rd, context.IAdd(AmpGpr(i.Ra), Const(i.Bits(40, 10))));
        }

        // ISBERD Rd, [Ra + imm]: as Maxwell's, the vertex index passes through for the following ALD
        public static void AmpIsberd(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            AmpSetDest(context, i.Rd, context.IAdd(AmpGpr(i.Ra), Const(i.Bits(40, 16))));
        }

        public static void AmpPixld(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            context.TranslatorContext.GpuAccessor.Log($"Ampere PIXLD of value {i.Bits(78, 3)} reads 0.");

            AmpSetDest(context, i.Rd, Const(0));
        }

        // OUT Rd, Ra, Sb: emit (bit 78) and / or cut (bit 79) a geometry shader vertex; Rd is the next output handle
        public static void AmpOut(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            int type = i.Bits(78, 2);

            EmitOut(context, (type & 1) != 0, (type & 2) != 0);

            AmpSetDest(context, i.Rd, AmpGpr(i.Ra));
        }

        public static void AmpKill(EmitterContext context)
        {
            context.Discard();
        }

        // ---- Textures

        // The coordinates and parameters of an Ampere texture instruction are read in Maxwell's order (Ra first, then Rb). With .SCR (bit 60), the compiler split one list of
        // n <= 4 values into Ra (2 when n >= 3, else 1) and Rb (the rest), so the list is read from both, in order (NAK's try_scalarize_tex).
        private static (Func<Operand> Ra, Func<Operand> Rb) AmpTexSources(EmitterContext context, AmpInst i, int scalarCount)
        {
            int ra = i.Ra, rb = i.Rb;

            Operand ReadA()
            {
                if (ra > RegisterConsts.RegisterZeroIndex)
                {
                    return Const(0);
                }

                return context.Copy(Register(ra++, RegisterType.Gpr));
            }

            Operand ReadB()
            {
                if (rb > RegisterConsts.RegisterZeroIndex)
                {
                    return Const(0);
                }

                return context.Copy(Register(rb++, RegisterType.Gpr));
            }

            if (!i.Bit(60))
            {
                return (ReadA, ReadB);
            }

            int fromA = scalarCount >= 3 ? 2 : 1;
            int read = 0;

            Operand ReadScalar()
            {
                return read++ < fromA ? ReadA() : ReadB();
            }

            return (ReadScalar, ReadScalar);
        }

        // Destinations: the first two components in the Rd pair, the others in the Rd2 pair (bits 64-71), as NAK's parse_tex
        private static Func<int, Operand[]> AmpTexDests(AmpInst i)
        {
            return count =>
            {
                Operand[] dests = new Operand[count];

                for (int c = 0; c < count; c++)
                {
                    int reg = c < 2 || i.Rc == RegisterConsts.RegisterZeroIndex ? i.Rd + c : i.Rc + c - 2;

                    dests[c] = reg >= RegisterConsts.RegisterZeroIndex || (c < 2 && i.Rd == RegisterConsts.RegisterZeroIndex) ? Local() : Register(reg, RegisterType.Gpr);
                }

                return dests;
            };
        }

        // Dimension bits 61-63: 0 1D, 1 2D, 2 3D, 3 cube, 4 1D array, 5 2D array, 7 cube array
        private static TexDim AmpTexDim(AmpInst i) => i.Bits(61, 3) switch
        {
            0 => TexDim._1d,
            1 => TexDim._2d,
            2 => TexDim._3d,
            3 => TexDim.Cube,
            4 => TexDim.Array1d,
            5 => TexDim.Array2d,
            _ => TexDim.ArrayCube,
        };

        // The bound handle: c[bank (54-58)][word (40-53) * 4]. Graphics programs keep NVN's handle table in c[2], the slot the translator reads by default.
        private static (int Handle, int CbufSlot) AmpTexHandle(AmpInst i)
        {
            int slot = i.Bits(54, 5);

            return (i.Bits(40, 14), slot == 2 ? TextureOperation.DefaultCbufSlot : slot);
        }

        private static int AmpTexCoords(TexDim dim) => dim switch
        {
            TexDim._1d => 1,
            TexDim.Array1d => 2,
            TexDim._2d => 2,
            TexDim.Array2d => 3,
            TexDim._3d => 3,
            TexDim.Cube => 3,
            _ => 4,
        };

        // LOD mode bits 87-89: 0 auto, 1 zero, 2 bias, 3 level, 4 clamp, 5 bias and clamp
        public static void AmpTex(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            bool bindless = i.Op12 != 0xb60;
            TexDim dim = AmpTexDim(i);
            int lodMode = i.Bits(87, 3);
            bool dc = i.Bit(78), aoffi = i.Bit(76);

            Lod lod = lodMode switch
            {
                1 => Lod.Lz,
                2 => Lod.Lb,
                3 => Lod.Ll,
                5 => Lod.Lba,
                _ => 0,
            };

            if (lodMode is 4 or 5)
            {
                context.TranslatorContext.GpuAccessor.Log("Ampere TEX with a LOD clamp: the clamp is ignored.");
            }

            int scalarCount = AmpTexCoords(dim) + (lod is Lod.Lb or Lod.Ll or Lod.Lba ? 1 : 0) + (dc ? 1 : 0) + (aoffi ? 1 : 0);
            (Func<Operand> ra, Func<Operand> rb) = AmpTexSources(context, i, scalarCount);
            (int handle, int cbufSlot) = AmpTexHandle(i);

            EmitTex(context, bindless ? TextureFlags.Bindless : TextureFlags.None, dim, lod, bindless ? 0 : handle, i.Bits(72, 4), ra, rb, AmpTexDests(i), false, dc, aoffi, cbufSlot);
        }

        // TLD: integer coordinates; LOD mode 1 zero, 3 level; .MS bit 78
        public static void AmpTld(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            bool bindless = i.Op12 != 0xb66;
            TexDim dim = AmpTexDim(i);
            bool ms = i.Bit(78), aoffi = i.Bit(76);
            Lod lod = i.Bits(87, 3) == 3 ? Lod.Ll : Lod.Lz;

            int scalarCount = AmpTexCoords(dim) + (lod == Lod.Ll ? 1 : 0) + (ms ? 1 : 0) + (aoffi ? 1 : 0);
            (Func<Operand> ra, Func<Operand> rb) = AmpTexSources(context, i, scalarCount);
            (int handle, int cbufSlot) = AmpTexHandle(i);

            TextureFlags flags = TextureFlags.IntCoords | (bindless ? TextureFlags.Bindless : TextureFlags.None);

            EmitTex(context, flags, dim, lod, bindless ? 0 : handle, i.Bits(72, 4), ra, rb, AmpTexDests(i), ms, false, aoffi, cbufSlot);
        }

        // TLD4.comp: component bits 87-88, .DC 78, offsets 76-77 (1 AOFFI, 2 PTP)
        public static void AmpTld4(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            bool bindless = i.Op12 != 0xb63;
            TexDim dim = AmpTexDim(i);
            TexOffset offset = (TexOffset)i.Bits(76, 2);
            bool dc = i.Bit(78);

            int scalarCount = AmpTexCoords(dim) + (dc ? 1 : 0) + (offset == TexOffset.Aoffi ? 1 : offset == TexOffset.Ptp ? 2 : 0);
            (Func<Operand> ra, Func<Operand> rb) = AmpTexSources(context, i, scalarCount);
            (int handle, int cbufSlot) = AmpTexHandle(i);

            EmitTld4(context, dim, (TexComp)i.Bits(87, 2), bindless ? 0 : handle, i.Bits(72, 4), ra, rb, AmpTexDests(i), offset, dc, bindless, cbufSlot);
        }

        public static void AmpTmml(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            bool bindless = i.Op12 != 0xb69;
            TexDim dim = AmpTexDim(i);
            (Func<Operand> ra, Func<Operand> rb) = AmpTexSources(context, i, AmpTexCoords(dim));
            (int handle, int cbufSlot) = AmpTexHandle(i);
            Func<int, Operand[]> dests = AmpTexDests(i);
            Operand[] all = dests(4);
            int next = 0;

            EmitTmml(context, dim, bindless ? 0 : handle, i.Bits(72, 4), ra, rb, () => next < all.Length ? all[next++] : null, bindless, cbufSlot);
        }

        // TXD: Ra holds the coordinates then the packed array index and offsets, Rb the derivatives (Maxwell's layout)
        public static void AmpTxd(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            bool bindless = i.Op12 != 0xb6c;
            TexDim dim = AmpTexDim(i);
            (Func<Operand> ra, Func<Operand> rb) = AmpTexSources(context, i, 0);
            (int handle, int cbufSlot) = AmpTexHandle(i);

            EmitTxd(context, dim, bindless ? 0 : handle, i.Bits(72, 4), ra, rb, AmpTexDests(i), i.Bit(76), bindless, cbufSlot);
        }

        // TXQ: query bits 62-63 (0 dimension, 1 texture type, 2 sampler position), the level in Ra
        public static void AmpTxq(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            bool bindless = i.Op12 != 0xb6f;
            (int handle, int cbufSlot) = AmpTexHandle(i);
            List<Operand> sources = [];
            int ra = i.Ra;

            Operand ReadA() => ra > RegisterConsts.RegisterZeroIndex ? Const(0) : context.Copy(Register(ra++, RegisterType.Gpr));

            if (bindless)
            {
                sources.Add(ReadA());
            }

            sources.Add(ReadA());

            TexQuery query = i.Bits(62, 2) switch
            {
                0 => TexQuery.TexHeaderDimension,
                1 => TexQuery.TexHeaderTextureType,
                _ => TexQuery.TexHeaderSamplerPos,
            };

            Operand[] all = AmpTexDests(i)(4);
            int next = 0;

            EmitTxq(context, query, bindless ? 0 : handle, i.Bits(72, 4), sources.ToArray(), () => next < all.Length ? all[next++] : null, bindless, cbufSlot);
        }

        // ---- Control flow

        // The branch condition: the guard (bits 12-15) and the condition predicate (87-90), both PT unless used
        private static Operand AmpBranchCondition(EmitterContext context, AmpInst i)
        {
            Operand guard = AmpPredSrc(context, i.GuardPred, i.GuardInv);
            Operand cond = AmpPredSrc(context, i.PredA, i.PredAInv);

            if (guard.Type == OperandType.Constant)
            {
                return guard.Value == IrConsts.True ? cond : guard;
            }

            return cond.Type == OperandType.Constant && cond.Value == IrConsts.True ? guard : context.BitwiseAnd(guard, cond);
        }

        public static void AmpBra(EmitterContext context)
        {
            AmpInst i = AmpOp(context);
            InstOp op = context.CurrOp;
            ulong target = op.GetAbsoluteAddress();

            if (target == op.Address + 16)
            {
                return;
            }

            Operand label = context.GetLabel(target);
            Operand cond = AmpBranchCondition(context, i);

            if (cond.Type == OperandType.Constant)
            {
                if (cond.Value == IrConsts.True)
                {
                    context.Branch(label);
                }

                return;
            }

            context.BranchIfTrue(label, cond);
        }

        // BRX: the decoder found the targets (AmpereDecoder.FindBrxTargets); each is compared with Ra + the base address, as Maxwell's BRX
        public static void AmpBrx(EmitterContext context)
        {
            AmpInst i = AmpOp(context);
            InstOp currOp = context.CurrOp;
            int startIndex = context.CurrBlock.HasNext() ? 1 : 0;

            if (context.CurrBlock.Successors.Count <= startIndex)
            {
                context.TranslatorContext.GpuAccessor.Log($"Failed to find targets for Ampere BRX instruction at 0x{currOp.Address:X}.");
                return;
            }

            Operand cond = AmpBranchCondition(context, i);
            Operand lblSkip = null;

            if (cond.Type != OperandType.Constant || cond.Value != IrConsts.True)
            {
                lblSkip = startIndex != 0 ? context.GetLabel(context.CurrBlock.Successors[0].Address) : Label();
                context.BranchIfFalse(lblSkip, cond);
            }

            Operand address = context.IAdd(i.Op12 == 0x958 ? AmpUr(i.URa) : AmpGpr(i.Ra), Const((int)currOp.GetAbsoluteAddress()));   // BRXU takes a UR

            List<Block> targets = context.CurrBlock.Successors.GetRange(startIndex, context.CurrBlock.Successors.Count - startIndex);
            targets.Sort((x, y) => y.Address.CompareTo(x.Address));

            ulong lastTargetAddress = ulong.MaxValue;

            for (int index = 0; index < targets.Count; index++)
            {
                Block target = targets[index];
                Operand label = context.GetLabel(target.Address);

                if (index < targets.Count - 1)
                {
                    if (target.Address != lastTargetAddress)
                    {
                        context.BranchIfTrue(label, context.ICompareEqual(address, Const((int)target.Address)));
                    }

                    lastTargetAddress = target.Address;
                }
                else
                {
                    context.Branch(label);
                }
            }

            if (lblSkip != null && startIndex == 0)
            {
                context.MarkLabel(lblSkip);
            }
        }

        public static void AmpCall(EmitterContext context)
        {
            DecodedFunction function = context.Program.GetFunctionByAddress(context.CurrOp.GetAbsoluteAddress());

            context.Call(function.Id, false);
        }

        public static void AmpRet(EmitterContext context)
        {
            AmpInst i = AmpOp(context);
            Operand cond = AmpBranchCondition(context, i);

            if (!context.IsNonMain)
            {
                context.TranslatorContext.GpuAccessor.Log("Ampere RET in the main function.");
                return;
            }

            AmpReturnIf(context, cond, () => context.Return());
        }

        public static void AmpExit(EmitterContext context)
        {
            AmpInst i = AmpOp(context);

            if (context.IsNonMain)
            {
                context.TranslatorContext.GpuAccessor.Log("Invalid exit on non-main function.");
                return;
            }

            AmpReturnIf(context, AmpBranchCondition(context, i), () =>
            {
                if (context.PrepareForReturn())
                {
                    context.Return();
                }
            });
        }

        private static void AmpReturnIf(EmitterContext context, Operand cond, Action emitReturn)
        {
            if (cond.Type == OperandType.Constant)
            {
                if (cond.Value == IrConsts.True)
                {
                    emitReturn();
                }

                return;
            }

            Operand lblSkip = Label();
            context.BranchIfFalse(lblSkip, cond);
            emitReturn();
            context.MarkLabel(lblSkip);
        }
    }
}
